"""Search stage: run a SearchQuery, store every job card (and its company) it returns.

LinkedIn serves at most ~1000 results for one search, however many match.
Before paginating a search we look at the page just below that cap; if it is
not empty, the search is "saturated" and we split it and run each part
instead, recursively:

1. by region of the country (SearchQuery.region_list()). Postings listed for
   the whole country ("United States", typical for remote jobs) are in no
   region, so the country-level search is then also collected split by
   facets only;
2. by workplace type, then job type: every job has exactly one value of each,
   so the parts together cover the same jobs;
3. by experience level as a last resort. Jobs without a level ("Not
   applicable") fall into no part, so the unsplit search is paginated too.

A slice that still reaches the cap with nothing left to split by is counted
in SearchRun.truncated_slices: data loss is visible, not silent.

Collected slices are recorded on the SearchRun, so a run stopped by rate
limiting or a crash resumes where it stopped instead of starting over.
"""

from __future__ import annotations

import logging

from django.db import connection, transaction
from django.utils import timezone

from jobs.linkedin.client import LinkedInClient, LinkedInError, RateLimited
from jobs.linkedin.parsers import JobCard, company_slug, parse_search_page
from jobs.models import Company, Job, SearchQuery, SearchRun, clip_to_fields

log = logging.getLogger(__name__)

# Default cap; the real one is measured with `linkedin_probe --find-cap` and set
# with LINKEDIN_RESULT_CAP. Saturation is probed at 90 % of it, for a margin.
RESULT_CAP = 1000
CAP_PROBE_START = 900


def _cap_settings() -> tuple[int, int]:
    from django.conf import settings

    cap = settings.LINKEDIN.get("RESULT_CAP", RESULT_CAP)
    return cap, max(10, cap * 9 // 100 * 10)


# Facet -> all its values, in the order we try splitting.
SPLIT_FACETS: list[tuple[str, list[str]]] = [
    ("f_WT", ["1", "2", "3"]),
    ("f_JT", ["F", "P", "C", "T", "I", "V", "O"]),
    ("f_E", ["1", "2", "3", "4", "5", "6"]),
]
# Facets some jobs have no value for; splitting by them alone can miss jobs.
LOSSY_FACETS = {"f_E"}
# pg_advisory_lock namespace: one running search per SearchQuery.
SEARCH_LOCK_NAMESPACE = 7231


class QueryBusy(Exception):
    """Another process is already running this query."""


CARD_FIELDS = ["url", "title", "company_name", "company_url", "location", "listed_at", "salary"]


def _slice_key(params: dict[str, str]) -> str:
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def _single_workplace_type(params: dict[str, str]) -> str:
    value = params.get("f_WT", "")
    return value if value in Job.WorkplaceType.values and value else ""


def _companies_by_url(cards: list[JobCard]) -> dict[str, Company]:
    # Sorted: concurrent inserts in a consistent order cannot deadlock on the unique index.
    urls = sorted({c.company_url for c in cards if c.company_url})
    if not urls:
        return {}
    Company.objects.bulk_create(
        [Company(linkedin_url=u, slug=company_slug(u)) for u in urls],
        ignore_conflicts=True,
    )
    return {c.linkedin_url: c for c in Company.objects.filter(linkedin_url__in=urls)}


def save_cards(query: SearchQuery, cards: list[JobCard], params: dict[str, str] | None = None) -> int:
    """Upsert cards and their companies, link jobs to the query. Returns how many jobs are new."""
    ids = [c.linkedin_id for c in cards]
    workplace_type = _single_workplace_type(params or {})
    now = timezone.now()
    with transaction.atomic():
        companies = _companies_by_url(cards)
        for company in companies.values():
            # Companies are created from the URL alone; give them a name until the detail stage runs.
            if not company.name:
                company.name = next(c.company_name for c in cards if c.company_url == company.linkedin_url)
                clip_to_fields(company)
                company.save(update_fields=["name"])

        existing = {j.linkedin_id: j for j in Job.objects.filter(linkedin_id__in=ids)}
        new = []
        for card in cards:
            company = companies.get(card.company_url)
            job = existing.get(card.linkedin_id)
            if job is None:
                job = Job(linkedin_id=card.linkedin_id, last_seen_at=now, workplace_type=workplace_type)
                for f in CARD_FIELDS:
                    setattr(job, f, getattr(card, f))
                job.company = company
                new.append(job)
            else:
                job.last_seen_at = now
                for f in CARD_FIELDS:
                    # Never overwrite a known value with an empty one from a sparser card.
                    if getattr(card, f):
                        setattr(job, f, getattr(card, f))
                job.company = company or job.company
                job.workplace_type = workplace_type or job.workplace_type
            clip_to_fields(job)
        # ignore_conflicts: another search process may insert the same job concurrently.
        Job.objects.bulk_create(sorted(new, key=lambda j: j.linkedin_id), ignore_conflicts=True)
        Job.objects.bulk_update(existing.values(), [*CARD_FIELDS, "company", "workplace_type", "last_seen_at"])
        query.jobs.add(*Job.objects.filter(linkedin_id__in=ids))
    return len(new)


class _SearchRunner:
    def __init__(self, query: SearchQuery, client: LinkedInClient, run: SearchRun, max_pages: int | None):
        self.query = query
        self.client = client
        self.run = run
        self.max_pages = max_pages
        self.country = query.base_params()["location"]
        self.regions = query.region_list()
        self.completed = set(run.completed_slices)
        self.cap, self.probe_start = _cap_settings()
        self.seen_before = run.jobs_seen  # from the interrupted part of a resumed run
        self.seen: set[int] = set()
        self.requests_before = client.requests_made - run.requests_made

    @property
    def requests_made(self) -> int:
        return self.client.requests_made - self.requests_before

    def collect(self, params: dict[str, str], use_regions: bool = True) -> bool:
        """Collect one slice, splitting it if needed. Returns False if cut short by --max-pages."""
        key = _slice_key(params) + ("" if use_regions else "#country-level")
        if key in self.completed:
            return True
        self.run.slices += 1
        split = self._split_options(params, use_regions)
        saturated = False
        if split:
            # Page 0 first: LinkedIn answers deep pages of a search nobody has opened yet
            # with an empty page, which would make a capped search look small.
            first_page = self._cards(params, 0)
            saturated = bool(first_page) and self._is_saturated(params)
            if not saturated:
                outcome = self._paginate(params, first_page=first_page, splittable=True)
                complete = outcome != "short"
                if outcome == "capped":
                    log.info("%s reached the cap although the probe missed it", _describe(params))
                    saturated = True
        else:
            complete = self._paginate(params) != "short"
        if saturated:
            facet, values = split
            log.info("%s is at the result cap, splitting by %s into %d", _describe(params), facet, len(values))
            complete = True
            for value in values:
                child = {**params, facet: value}
                if facet == "location":
                    child.pop("geoId", None)  # the geoId would override the region
                complete &= self.collect(child, use_regions)
            if facet == "location":
                # Jobs listed for the whole country are in no region: collect them by facets.
                complete &= self.collect(params, use_regions=False)
            elif facet in LOSSY_FACETS:
                # Jobs without a value for this facet are in no part: take what the unsplit search shows.
                complete &= self._paginate(params, fallback=True) != "short"
        if complete:
            self.completed.add(key)
            self.run.completed_slices = sorted(self.completed)
            self._save_progress()
        return complete

    def _split_options(self, params: dict[str, str], use_regions: bool) -> tuple[str, list[str]] | None:
        """Next dimension the search can be split on, and the values to split into."""
        if use_regions and self.regions and params.get("location") == self.country:
            return "location", self.regions
        for facet, all_values in SPLIT_FACETS:
            current = [v for v in params.get(facet, "").split(",") if v]
            values = current or all_values
            if len(values) > 1:
                return facet, values
        return None

    def _is_saturated(self, params: dict[str, str]) -> bool:
        return bool(self._cards(params, self.probe_start))

    def _cards(self, params: dict[str, str], start: int) -> list[JobCard]:
        """One page of cards. LinkedIn now and then returns an empty page where there
        are results, so an empty page is requested once more before it is believed."""
        cards = parse_search_page(self.client.search_page(params, start))
        if not cards:
            cards = parse_search_page(self.client.search_page(params, start))
        return cards

    def _paginate(
        self,
        params: dict[str, str],
        fallback: bool = False,
        first_page: list[JobCard] | None = None,
        splittable: bool = False,
    ) -> str:
        """Collect every page of one slice.

        Returns "short" if stopped early by --max-pages, "capped" if it reached the
        result cap, else "done". Reaching the cap counts as truncation unless the
        caller can still split the slice (`splittable`).
        `fallback`: the slice's parts were already collected; this pass only looks
        for jobs missing from them, so reaching the cap means loss only if it found some.
        `first_page`: page 0, if the caller already has it.
        """
        start = pages = 0
        missed_by_parts = 0
        seen: set[int] = set()
        repeated_pages = 0
        while start < self.cap:
            cards = first_page if start == 0 and first_page is not None else self._cards(params, start)
            if not cards:
                break
            fresh = {c.linkedin_id for c in cards} - seen
            missed_by_parts += len(fresh - self.seen)
            seen |= fresh
            self.seen |= fresh
            self.run.jobs_new += save_cards(self.query, cards, params)
            self._save_progress()
            pages += 1
            start += len(cards)
            if self.max_pages and pages >= self.max_pages:
                return "short"
            # LinkedIn sometimes repeats the last page instead of ending.
            repeated_pages = 0 if fresh else repeated_pages + 1
            if repeated_pages >= 2:
                break
        capped = start >= self.probe_start
        if capped and splittable:
            return "capped"
        if capped and (missed_by_parts or not fallback):
            self.run.truncated_slices += 1
            log.warning("%s reaches the result cap with nothing left to split by; jobs beyond it are missed",
                        _describe(params))  # fmt: skip
        log.info("%s: %d pages, %d jobs", _describe(params), pages, len(seen))
        return "capped" if capped else "done"

    def _save_progress(self, final: bool = False) -> None:
        self.run.requests_made = self.requests_made
        if final:
            # Exact count, including jobs seen before a resume and seen in several slices.
            self.run.jobs_seen = self.query.jobs.filter(last_seen_at__gte=self.run.started_at).count()
        else:
            self.run.jobs_seen = self.seen_before + len(self.seen)
        self.run.save()


def _describe(params: dict[str, str]) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items() if k not in ("keywords", "sortBy"))


def _resumable_run(query: SearchQuery) -> SearchRun | None:
    last = query.runs.order_by("-started_at").first()
    if last and last.status != SearchRun.Status.DONE:
        return last
    return None


def _advisory_lock(query: SearchQuery, lock: bool) -> bool:
    sql = "SELECT pg_try_advisory_lock(%s, %s)" if lock else "SELECT pg_advisory_unlock(%s, %s)"
    with connection.cursor() as cursor:
        cursor.execute(sql, [SEARCH_LOCK_NAMESPACE, query.pk])
        return cursor.fetchone()[0]


def run_search(
    query: SearchQuery, client: LinkedInClient, max_pages: int | None = None, restart: bool = False
) -> SearchRun:
    """Run one query to completion, or until LinkedIn throttles us.

    An unfinished previous run of the query is continued unless `restart`.
    Raises QueryBusy if another process is running the same query.
    """
    if not _advisory_lock(query, lock=True):
        raise QueryBusy(f"{query} is already being searched by another process")
    try:
        return _run_search(query, client, max_pages, restart)
    finally:
        _advisory_lock(query, lock=False)


def _run_search(query: SearchQuery, client: LinkedInClient, max_pages: int | None, restart: bool) -> SearchRun:
    run = None if restart else _resumable_run(query)
    if run is None:
        run = SearchRun.objects.create(query=query)
    else:
        log.info("resuming run #%d of %s (%d slices already done)", run.pk, query, len(run.completed_slices))
        run.status, run.error, run.finished_at = SearchRun.Status.RUNNING, "", None
        run.save()
    runner = _SearchRunner(query, client, run, max_pages)
    try:
        runner.collect(query.base_params())
        run.status = SearchRun.Status.DONE
    except RateLimited as exc:
        run.status = SearchRun.Status.BLOCKED
        run.error = str(exc)
    except LinkedInError as exc:
        run.status = SearchRun.Status.FAILED
        run.error = str(exc)
    except Exception as exc:
        run.status = SearchRun.Status.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        run.finished_at = timezone.now()
        runner._save_progress(final=True)
        query.last_run_at = run.finished_at
        query.save(update_fields=["last_run_at"])
    return run

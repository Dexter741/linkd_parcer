"""Detail stage: fetch full job postings and company pages found by the search stage.

It shares nothing with the search stage except the database. The Job and
Company tables work as queues: `detail_status` says what still needs
fetching. Workers claim a batch with SELECT ... FOR UPDATE SKIP LOCKED, so
several can run side by side (for example one per proxy) without fetching
the same record twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Model, OuterRef, Q, Subquery
from django.utils import timezone

from jobs.linkedin.client import LinkedInClient, RateLimited
from jobs.linkedin.parsers import company_slug, parse_company_page, parse_job_detail
from jobs.models import Company, DetailQueue, Job, clip_to_fields

log = logging.getLogger(__name__)

# A claim older than this belongs to a worker that died; the record is up for grabs again.
STALE_CLAIM = timedelta(minutes=30)

Status = DetailQueue.DetailStatus

JOB_DETAIL_FIELDS = [
    "description_html",
    "description_text",
    "seniority_level",
    "employment_type",
    "job_function",
    "industries",
    "applicants",
    "posted_ago",
    "apply_url",
    "is_closed",
]
# Card fields the detail page may fill when the search card lacked them.
JOB_FILLABLE_FIELDS = ["title", "company_name", "location", "salary"]
COMPANY_FIELDS = [
    "name",
    "tagline",
    "description",
    "website",
    "industry",
    "company_size",
    "headquarters",
    "address",
    "company_type",
    "founded",
    "specialties",
    "followers",
    "employees_on_linkedin",
    "logo_url",
]
# Only the companies of jobs whose details were fetched (for a partial run).
COMPANIES_OF_DONE_JOBS = Q(pk__in=Job.objects.filter(detail_status=Status.DONE).values("company_id"))

QUEUE_FIELDS = ["detail_status", "detail_attempts", "detail_error", "detail_claimed_at", "detail_fetched_at"]


@dataclass
class DetailStats:
    done: int = 0
    gone: int = 0
    failed: int = 0
    stopped_by_rate_limit: bool = False

    @property
    def processed(self) -> int:
        return self.done + self.gone + self.failed


def _claimable(retry_failed: bool) -> Q:
    max_attempts = settings.LINKEDIN["DETAIL_MAX_ATTEMPTS"]
    q = Q(detail_status=Status.PENDING)
    q |= Q(detail_status=Status.IN_PROGRESS, detail_claimed_at__lt=timezone.now() - STALE_CLAIM)
    if retry_failed:
        q |= Q(detail_status=Status.FAILED)
    else:
        q |= Q(detail_status=Status.FAILED, detail_attempts__lt=max_attempts)
    return q


def _claim_order(model: type[DetailQueue]) -> list:
    """Fewest attempts first (so a page that keeps failing goes to the back); for
    companies, then the ones with the most jobs, so a partial run covers most jobs."""
    if model is not Company:
        return ["detail_attempts", "first_seen_at"]
    job_count = Subquery(
        Job.objects.filter(company=OuterRef("pk")).values("company").annotate(n=Count("pk")).values("n")[:1]
    )
    return ["detail_attempts", job_count.desc(nulls_last=True), "first_seen_at"]


def claim_batch(
    model: type[DetailQueue],
    size: int,
    retry_failed: bool = False,
    exclude_ids: set[int] = frozenset(),
    scope: Q | None = None,
) -> list:
    with transaction.atomic():
        records = list(
            model.objects.select_for_update(skip_locked=True)
            .filter(_claimable(retry_failed))
            .filter(scope or Q())
            .exclude(pk__in=exclude_ids)
            .order_by(*_claim_order(model))[:size]
        )
        if records:
            model.objects.filter(pk__in=[r.pk for r in records]).update(
                detail_status=Status.IN_PROGRESS, detail_claimed_at=timezone.now()
            )
    return records


def _release(model: type[DetailQueue], records: list) -> None:
    model.objects.filter(pk__in=[r.pk for r in records], detail_status=Status.IN_PROGRESS).update(
        detail_status=Status.PENDING, detail_claimed_at=None
    )


def _requeue_throttled(record: DetailQueue, exc: Exception) -> None:
    """Put the record that hit throttling back in the queue, behind the others.

    Counting the attempt sends it to the end of the queue (claims are ordered by
    attempts), so one page that always shows the login wall cannot block the
    queue; after the maximum attempts it is marked failed.
    """
    record.detail_status = (
        Status.FAILED if record.detail_attempts >= settings.LINKEDIN["DETAIL_MAX_ATTEMPTS"] else Status.PENDING
    )
    record.detail_error = f"throttled: {exc}"
    record.save(update_fields=QUEUE_FIELDS)


def _apply_job_detail(job: Job, html: str) -> list[str]:
    detail = parse_job_detail(html)
    for name in JOB_DETAIL_FIELDS:
        setattr(job, name, getattr(detail, name))
    changed = list(JOB_DETAIL_FIELDS)
    for name in JOB_FILLABLE_FIELDS:
        if not getattr(job, name) and getattr(detail, name):
            setattr(job, name, getattr(detail, name))
            changed.append(name)
    if job.company_id is None and detail.company_url:
        job.company, _ = Company.objects.get_or_create(
            linkedin_url=detail.company_url,
            defaults={"slug": company_slug(detail.company_url), "name": detail.company_name[:300]},
        )
        job.company_url = detail.company_url
        changed += ["company", "company_url"]
    return changed


def _apply_company_page(company: Company, html: str) -> list[str]:
    page = parse_company_page(html)
    for name in COMPANY_FIELDS:
        value = getattr(page, name)
        if value not in ("", None):
            setattr(company, name, value)
    return list(COMPANY_FIELDS)


def fetch_one(record: Job | Company, client: LinkedInClient) -> str:
    """Fetch and store one record's details. Returns the resulting status.

    RateLimited is not handled here: the caller must stop, not mark the record failed.
    Any other problem only fails this record.
    """
    record.detail_attempts += 1
    record.detail_claimed_at = None
    record.detail_fetched_at = timezone.now()
    changed: list[str] = []
    try:
        if isinstance(record, Job):
            html = client.job_detail(record.linkedin_id)
            apply = _apply_job_detail
        else:
            html = client.company_page(record.linkedin_url)
            apply = _apply_company_page
        if html is None:
            record.detail_status = Status.GONE
            record.detail_error = "LinkedIn returned 404"
        else:
            changed = apply(record, html)
            record.detail_status = Status.DONE
            record.detail_error = ""
    except RateLimited:
        raise
    except Exception as exc:
        log.debug("detail fetch failed", exc_info=True)
        record.detail_status = Status.FAILED
        record.detail_error = f"{type(exc).__name__}: {exc}"
        changed = []
    clip_to_fields(record)
    # update_fields: a search running at the same time may be updating the card fields.
    record.save(update_fields=[*changed, *QUEUE_FIELDS])
    return record.detail_status


def run_details(
    client: LinkedInClient,
    model: type[DetailQueue] = Job,
    limit: int | None = None,
    batch_size: int = 20,
    retry_failed: bool = False,
    scope: Q | None = None,
) -> DetailStats:
    """`scope` narrows which records are processed, e.g. COMPANIES_OF_DONE_JOBS."""
    stats = DetailStats()
    # Records handled in this run; one that fails is not picked up again in the same run.
    handled: set[int] = set()
    label = model._meta.verbose_name_plural
    while limit is None or stats.processed < limit:
        size = batch_size if limit is None else min(batch_size, limit - stats.processed)
        batch = claim_batch(model, size, retry_failed=retry_failed, exclude_ids=handled, scope=scope)
        if not batch:
            break
        for i, record in enumerate(batch):
            handled.add(record.pk)
            # Keep our claim fresh so a slow batch is not mistaken for a dead worker's.
            model.objects.filter(pk__in=[r.pk for r in batch[i:]], detail_status=Status.IN_PROGRESS).update(
                detail_claimed_at=timezone.now()
            )
            try:
                status = fetch_one(record, client)
            except RateLimited as exc:
                log.warning("stopping: %s", exc)
                _requeue_throttled(record, exc)
                _release(model, batch[i + 1 :])
                stats.stopped_by_rate_limit = True
                return stats
            except KeyboardInterrupt:
                # Ctrl+C: hand the unprocessed rest of the batch back now, not after STALE_CLAIM.
                _release(model, batch[i:])
                raise
            if status == Status.DONE:
                stats.done += 1
            elif status == Status.GONE:
                stats.gone += 1
            else:
                stats.failed += 1
                log.warning("%s failed: %s", _label(record), record.detail_error)
        log.info("%s: %d done, %d gone, %d failed", label, stats.done, stats.gone, stats.failed)
    return stats


def _label(record: Model) -> str:
    return f"job {record.linkedin_id}" if isinstance(record, Job) else f"company {record.linkedin_url}"

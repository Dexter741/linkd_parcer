from pathlib import Path

from jobs.linkedin.client import RateLimited

FIXTURES = Path(__file__).parent / "fixtures"
COUNTRY = "United States"


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text()


def card_html(job_id: int, company: str) -> str:
    return (
        f'<li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">'
        f'<h3 class="base-search-card__title">Python Developer {job_id}</h3>'
        f'<h4 class="base-search-card__subtitle">'
        f'<a href="https://www.linkedin.com/company/{company}?trk=public_jobs">Co {company}</a></h4>'
        f'<span class="job-search-card__location">Austin, TX</span>'
        f'<time class="job-search-card__listdate" datetime="2026-10-01"></time></div></li>'
    )


def make_jobs(n, start_id=1, regions=None, companies=7, **fixed):
    """Synthetic jobs: id -> facet values, spread evenly over facets and regions."""
    regions = regions or [f"{s}, {COUNTRY}" for s in ("Texas", "California", "New York")]
    wt, jt, e = ["1", "2", "3"], ["F", "P", "C"], ["2", "3", "4"]
    jobs = {}
    for i in range(n):
        jobs[start_id + i] = {
            "location": fixed.get("location", regions[i % len(regions)]),
            "f_WT": fixed.get("f_WT", wt[(i // len(regions)) % 3]),
            "f_JT": fixed.get("f_JT", jt[(i // 9) % 3]),
            "f_E": fixed.get("f_E", e[(i // 27) % 3]),
            "company": f"c{(start_id + i) % companies}",
        }
    return jobs


class FakeLinkedIn:
    """In-memory stand-in for LinkedIn's logged-out pages.

    Like LinkedIn it serves 10 cards per page and never more than `cap` per
    search. A search for the country matches every region.
    """

    def __init__(self, jobs: dict[int, dict[str, str]], cap: int = 1000, page_size: int = 10):
        self.jobs = jobs
        self.cap = cap
        self.page_size = page_size
        self.requests_made = 0
        self.search_calls: list[tuple[dict, int]] = []
        self.detail_responses: dict[int, object] = {}
        self.company_responses: dict[str, object] = {}
        self.throttle_after: int | None = None
        self.flaky_empty: set[int] = set()  # starts that answer empty once, then normally
        # Like LinkedIn: deep pages of a search whose page 0 was never requested come back empty.
        self.cold_deep_pages_empty = False
        self._opened: set[str] = set()

    def _count(self):
        self.requests_made += 1
        if self.throttle_after is not None and self.requests_made > self.throttle_after:
            raise RateLimited("HTTP 429")

    def matches(self, params):
        def ok(facets):
            if params["location"] != COUNTRY and facets["location"] != params["location"]:
                return False
            return all(not params.get(f) or facets.get(f) in params[f].split(",") for f in ("f_WT", "f_JT", "f_E"))

        return [job_id for job_id, facets in sorted(self.jobs.items()) if ok(facets)]

    def search_page(self, params, start):
        self._count()
        self.search_calls.append((dict(params), start))
        key = repr(sorted(params.items()))
        if start == 0:
            self._opened.add(key)
        elif self.cold_deep_pages_empty and start >= 100 and key not in self._opened:
            return ""
        if start in self.flaky_empty:
            self.flaky_empty.discard(start)
            return ""
        ids = self.matches(params)[: self.cap]
        return "".join(card_html(i, self.jobs[i]["company"]) for i in ids[start : start + self.page_size])

    def _respond(self, responses, key, default):
        self._count()
        response = responses.get(key, default)
        if isinstance(response, BaseException):
            raise response
        return response

    def job_detail(self, job_id):
        return self._respond(self.detail_responses, job_id, fixture_html("job_detail.html"))

    def company_page(self, url):
        return self._respond(self.company_responses, url, fixture_html("company_page.html"))

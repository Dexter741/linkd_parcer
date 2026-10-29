"""HTTP access to LinkedIn's public (logged-out) pages.

The job endpoints are the ones the logged-out jobs page itself calls (found in
DevTools → Network); they return HTML fragments and need no account or
cookies. Company data comes from the public company page.

The client is deliberately slow: a random pause before every request and
exponential backoff when LinkedIn pushes back (HTTP 429, its non-standard 999
"request denied", or a redirect to the login wall).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# 999 is LinkedIn's "request denied" code for suspected bots.
THROTTLE_STATUSES = {429, 999}
RETRY_STATUSES = THROTTLE_STATUSES | {500, 502, 503, 504}
# Logged-out visitors LinkedIn wants to get rid of are redirected here.
LOGIN_WALL_MARKERS = ("/authwall", "/login", "/uas/login", "/checkpoint", "/signup")


class LinkedInError(Exception):
    pass


class RateLimited(LinkedInError):
    """LinkedIn kept throttling us after all retries. Stop and resume later."""


@dataclass
class ClientConfig:
    min_delay: float = 2.0
    max_delay: float = 5.0
    max_retries: int = 4
    backoff_base: float = 30.0
    timeout: float = 30.0
    proxy: str = ""

    @classmethod
    def from_settings(cls) -> ClientConfig:
        from django.conf import settings

        s = settings.LINKEDIN
        return cls(
            min_delay=s["MIN_DELAY"],
            max_delay=s["MAX_DELAY"],
            max_retries=s["MAX_RETRIES"],
            backoff_base=s["BACKOFF_BASE"],
            timeout=s["TIMEOUT"],
            proxy=s["PROXY"],
        )


class LinkedInClient:
    def __init__(
        self,
        config: ClientConfig | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config or ClientConfig()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        if self.config.proxy:
            self.session.proxies = {"http": self.config.proxy, "https": self.config.proxy}
        self._sleep = sleep
        self._last_request_at: float | None = None
        self.requests_made = 0

    def search_page(self, params: dict[str, str], start: int) -> str:
        """One page of search cards (about 10). Empty string means no more results."""
        resp = self._get(SEARCH_URL, {**params, "start": str(start)})
        # Past the last page LinkedIn answers 400 or an empty 200; both mean "done".
        if resp is None or resp.status_code == 400:
            return ""
        return resp.text

    def job_detail(self, job_id: int) -> str | None:
        """Detail HTML of one posting, or None if LinkedIn no longer has it."""
        return self._get_page(DETAIL_URL.format(job_id=job_id))

    def company_page(self, url: str) -> str | None:
        """Public company page HTML, or None if the company page does not exist."""
        return self._get_page(url)

    def _get_page(self, url: str) -> str | None:
        resp = self._get(url)
        if resp is None:
            return None
        if resp.status_code == 400:
            raise LinkedInError(f"HTTP 400 for {url}")
        return resp.text

    def _get(self, url: str, params: dict[str, str] | None = None) -> requests.Response | None:
        """GET with pacing and retries. Returns None on 404/410."""
        problem = ""
        throttled = False
        retry_after = 0.0
        for attempt in range(self.config.max_retries + 1):
            if attempt:
                self._backoff(attempt, problem, retry_after)
            self._pace()
            retry_after = 0.0
            try:
                resp = self.session.get(url, params=params, timeout=self.config.timeout)
            except requests.RequestException as exc:
                problem = f"network error: {exc}"
                continue
            finally:
                self.requests_made += 1
                self._last_request_at = time.monotonic()

            if resp.status_code in (404, 410):
                return None
            if resp.status_code in RETRY_STATUSES:
                problem = f"HTTP {resp.status_code}"
                throttled = throttled or resp.status_code in THROTTLE_STATUSES
                header = resp.headers.get("Retry-After", "")
                retry_after = float(header) if header.isdigit() else 0.0
                continue
            if _is_login_wall(resp):
                problem = "redirected to the login wall"
                throttled = True
                continue
            if resp.status_code > 400:
                raise LinkedInError(f"HTTP {resp.status_code} for {resp.url}")
            return resp

        if throttled:
            raise RateLimited(f"LinkedIn is throttling us ({problem}) after {self.config.max_retries} retries")
        raise LinkedInError(f"giving up on {url}: {problem}")

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        wanted = random.uniform(self.config.min_delay, self.config.max_delay)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wanted:
            self._sleep(wanted - elapsed)

    def _backoff(self, attempt: int, problem: str, retry_after: float) -> None:
        delay = self.config.backoff_base * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
        delay = max(delay, retry_after)
        log.warning("%s, retry %d/%d in %.0fs", problem, attempt, self.config.max_retries, delay)
        self._sleep(delay)


def _is_login_wall(resp: requests.Response) -> bool:
    urls = [r.headers.get("Location", "") for r in getattr(resp, "history", [])] + [resp.url or ""]
    return any(urlparse(u).path.startswith(LOGIN_WALL_MARKERS) for u in urls)

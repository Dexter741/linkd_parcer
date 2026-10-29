from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from jobs.linkedin.client import ClientConfig, LinkedInClient, LinkedInError
from jobs.linkedin.parsers import ParseError, parse_company_page, parse_job_detail, parse_search_page


class Command(BaseCommand):
    help = (
        "Check that LinkedIn is reachable and the parsers still match its markup: fetches one search page, "
        "one job, its company page and the pages around the result cap. Saves nothing to the database."
    )

    def add_arguments(self, parser):
        parser.add_argument("--keywords", default="Python Developer")
        parser.add_argument("--country", default="United States")
        parser.add_argument("--save-html", metavar="DIR", help="Also save the raw responses here.")
        parser.add_argument(
            "--find-cap", action="store_true", help="Only measure how many results LinkedIn serves per search."
        )

    def handle(self, *args, **opts):
        config = ClientConfig.from_settings()
        config.max_retries = 0  # a probe should answer fast, not wait out backoff
        self.client = LinkedInClient(config)
        self.save_dir = Path(opts["save_html"]) if opts["save_html"] else None
        try:
            if opts["find_cap"]:
                self._find_cap({"keywords": opts["keywords"], "location": opts["country"], "sortBy": "DD"})
            else:
                self._probe(opts)
        except LinkedInError as exc:
            raise CommandError(f"LinkedIn not reachable or refusing: {exc}") from exc

    def _probe(self, opts):
        params = {"keywords": opts["keywords"], "location": opts["country"], "sortBy": "DD"}

        html = self._save("search.html", self.client.search_page(params, 0))
        cards = parse_search_page(html)
        self.stdout.write(f"Search page 1: {len(cards)} cards")
        for card in cards[:3]:
            self.stdout.write(f"  {card}")
        if not cards:
            raise CommandError("No cards parsed. Look at the saved HTML: blocked, or the markup changed.")

        card = next((c for c in cards if c.company_url), cards[0])
        detail_html = self._save("job.html", self.client.job_detail(card.linkedin_id) or "")
        self.stdout.write(f"Job {card.linkedin_id}:")
        try:
            detail = parse_job_detail(detail_html)
            for name in ("title", "company_name", "company_url", "location", "posted_ago", "applicants", "salary"):
                self.stdout.write(f"  {name}: {getattr(detail, name)!r}")
            self.stdout.write(f"  apply_url: {detail.apply_url!r}")
            self.stdout.write(f"  criteria: {detail.criteria}")
            self.stdout.write(f"  description: {len(detail.description_text)} chars")
        except ParseError as exc:
            self.stderr.write(f"  NOT PARSED: {exc}")

        if card.company_url:
            company_html = self._save("company.html", self.client.company_page(card.company_url) or "")
            self.stdout.write(f"Company {card.company_url}:")
            try:
                for name, value in vars(parse_company_page(company_html)).items():
                    shown = f"{len(value)} chars" if name == "description" else repr(value)
                    self.stdout.write(f"  {name}: {shown}")
            except ParseError as exc:
                self.stderr.write(f"  NOT PARSED: {exc}")

        self.stdout.write(f"Requests made: {self.client.requests_made}")

    def _has_cards(self, params, start: int) -> bool:
        found = len(parse_search_page(self.client.search_page(params, start)))
        self.stdout.write(f"  start={start}: {found} cards")
        return found > 0

    def _find_cap(self, params):
        """Largest `start` that still returns cards, to the nearest 10 (about 15 requests)."""
        self.stdout.write("Use a search with many thousands of results (the default one is).")
        low, high = 0, None
        for start in range(100, 1100, 100):
            if not self._has_cards(params, start):
                high = start
                break
            low = start
        if high is None:
            self.stdout.write(
                f"Cards still at start={low}: the cap is at least {low + 10}; keep LINKEDIN_RESULT_CAP=1000."
            )
            return
        while high - low > 10:
            mid = (low + high) // 20 * 10
            if self._has_cards(params, mid):
                low = mid
            else:
                high = mid
        self.stdout.write(f"Last page with cards starts at {low}. Put this in .env: LINKEDIN_RESULT_CAP={high}")
        self.stdout.write(f"Requests made: {self.client.requests_made}")

    def _save(self, name: str, html: str) -> str:
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            (self.save_dir / name).write_text(html)
        return html

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.linkedin.client import ClientConfig, LinkedInClient
from jobs.models import SearchQuery, SearchRun
from jobs.services.search import QueryBusy, run_search


class Command(BaseCommand):
    help = (
        "Search stage: run search queries and store the jobs and companies they return. "
        "An interrupted run of a query is resumed where it stopped."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--query", type=int, action="append", dest="query_ids", help="SearchQuery id (repeatable).")
        target.add_argument("--all", action="store_true", help="Every active SearchQuery.")
        target.add_argument("--keywords", help="Ad-hoc search; creates a SearchQuery with --country.")
        parser.add_argument("--country", default="United States")
        parser.add_argument("--restart", action="store_true", help="Start over instead of resuming an unfinished run.")
        parser.add_argument("--max-pages", type=int, help="Stop each slice after N pages (for trial runs).")
        parser.add_argument(
            "--wait-on-block",
            type=int,
            default=0,
            metavar="MINUTES",
            help="When LinkedIn throttles, pause this long and resume instead of stopping.",
        )

    def handle(self, *args, **opts):
        settings.DEBUG = False  # with DEBUG on, Django keeps every SQL query in memory during long runs
        if opts["keywords"]:
            query = SearchQuery.objects.filter(
                keywords__iexact=opts["keywords"], country__iexact=opts["country"]
            ).first() or SearchQuery.objects.create(keywords=opts["keywords"], country=opts["country"])
            queries = [query]
        elif opts["all"]:
            queries = list(SearchQuery.objects.filter(is_active=True))
        else:
            queries = list(SearchQuery.objects.filter(pk__in=opts["query_ids"]))
        if not queries:
            raise CommandError("No search queries to run.")

        client = LinkedInClient(ClientConfig.from_settings())
        for query in queries:
            self.stdout.write(f"Searching: {query}")
            restart = opts["restart"]
            while True:
                try:
                    run = run_search(query, client, max_pages=opts["max_pages"], restart=restart)
                except QueryBusy as exc:
                    self.stderr.write(f"  skipped: {exc}")
                    run = None
                    break
                restart = False  # a resumed attempt continues this run
                self.stdout.write(
                    f"  {run.get_status_display()}: {run.jobs_seen} jobs ({run.jobs_new} new), "
                    f"{run.slices} slices, {run.requests_made} requests"
                    + (f", {run.truncated_slices} truncated" if run.truncated_slices else "")
                )
                if run.error:
                    self.stderr.write(f"  {run.error}")
                if run.status != SearchRun.Status.BLOCKED or not opts["wait_on_block"]:
                    break
                self.stderr.write(f"  LinkedIn is throttling; pausing {opts['wait_on_block']} min, then resuming.")
                time.sleep(opts["wait_on_block"] * 60)
            if run and run.status == SearchRun.Status.BLOCKED:
                self.stderr.write("LinkedIn is throttling; stopping. Re-run later to resume, collected jobs are saved.")
                break

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.linkedin.client import ClientConfig, LinkedInClient
from jobs.models import Company, Job
from jobs.services.details import COMPANIES_OF_DONE_JOBS, run_details

TARGETS = {"jobs": [Job], "companies": [Company], "all": [Job, Company]}
# With --follow, jobs and companies are processed in turns of this many records.
FOLLOW_CHUNK = 50
FOLLOW_POLL_SECONDS = 60


class Command(BaseCommand):
    help = "Detail stage: fetch full job postings and company pages for records found by the search stage."

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=["jobs", "companies"], help="Process only jobs or only companies.")
        parser.add_argument("--limit", type=int, help="Process at most N records of each kind.")
        parser.add_argument("--batch-size", type=int, default=20)
        parser.add_argument(
            "--retry-failed", action="store_true", help="Also retry records that already used up their attempts."
        )
        parser.add_argument(
            "--companies-of-done-jobs",
            action="store_true",
            help="Process only companies of jobs whose details were fetched (after a --limit run on jobs).",
        )
        parser.add_argument(
            "--follow",
            action="store_true",
            help="Keep running while the search part is still adding records, instead of exiting on an empty queue.",
        )
        parser.add_argument(
            "--idle-minutes", type=int, default=30, help="With --follow: exit after this long with nothing to do."
        )
        parser.add_argument(
            "--wait-on-block",
            type=int,
            default=0,
            metavar="MINUTES",
            help="When LinkedIn throttles, pause this long and continue instead of stopping.",
        )

    def handle(self, *args, **opts):
        settings.DEBUG = False  # with DEBUG on, Django keeps every SQL query in memory during long runs
        if opts["follow"] and opts["retry_failed"]:
            raise CommandError("--retry-failed cannot be combined with --follow (it would retry failures forever).")
        client = LinkedInClient(ClientConfig.from_settings())
        targets = TARGETS[opts["only"] or "all"]
        follow = opts["follow"]
        last_work = time.monotonic()

        while True:
            did_work = False
            for model in targets:
                remaining = None if follow else opts["limit"]
                while True:
                    stats = run_details(
                        client,
                        model,
                        limit=FOLLOW_CHUNK if follow else remaining,
                        batch_size=opts["batch_size"],
                        retry_failed=opts["retry_failed"],
                        scope=COMPANIES_OF_DONE_JOBS if model is Company and opts["companies_of_done_jobs"] else None,
                    )
                    did_work |= stats.processed > 0
                    if stats.processed or not follow:
                        label = model._meta.verbose_name_plural.capitalize()
                        self.stdout.write(
                            f"{label}: {stats.done} done, {stats.gone} removed from LinkedIn, {stats.failed} failed"
                        )
                    if not stats.stopped_by_rate_limit:
                        break
                    if not opts["wait_on_block"]:
                        self.stderr.write(
                            "Stopped: LinkedIn is throttling. Re-run later; unfinished records went back to the queue."
                        )
                        return
                    self.stderr.write(f"LinkedIn is throttling; pausing {opts['wait_on_block']} min.")
                    time.sleep(opts["wait_on_block"] * 60)
                    did_work = True  # a pause is not idleness
                    if follow:
                        break  # the follow loop comes back to this model
                    # Without --follow, carry on with the same kind of record after the pause.
                    if remaining is not None:
                        remaining -= stats.processed
                        if remaining <= 0:
                            break
            if not follow:
                return
            if did_work:
                last_work = time.monotonic()
            elif time.monotonic() - last_work > opts["idle_minutes"] * 60:
                self.stdout.write(f"Queue empty for {opts['idle_minutes']} min; exiting.")
                return
            else:
                time.sleep(FOLLOW_POLL_SECONDS)

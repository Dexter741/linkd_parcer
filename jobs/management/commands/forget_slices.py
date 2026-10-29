from django.core.management.base import BaseCommand, CommandError

from jobs.models import SearchRun


class Command(BaseCommand):
    help = (
        "Mark finished slices of the unfinished search run as not done, so resuming collects them again. "
        "Each argument is matched against the slice's location, e.g. 'Alabama, United States'."
    )

    def add_arguments(self, parser):
        parser.add_argument("locations", nargs="+")
        parser.add_argument("--query", type=int, help="SearchQuery id (default: the latest unfinished run).")

    def handle(self, *args, **opts):
        runs = SearchRun.objects.exclude(status=SearchRun.Status.DONE).order_by("-started_at")
        if opts["query"]:
            runs = runs.filter(query_id=opts["query"])
        run = runs.first()
        if run is None:
            raise CommandError("No unfinished search run.")
        markers = [f"location={loc.strip()}&" for loc in opts["locations"] if loc.strip()]
        keep = [k for k in run.completed_slices if not any(m in k for m in markers)]
        removed = len(run.completed_slices) - len(keep)
        run.completed_slices = keep
        run.truncated_slices = 0
        run.save(update_fields=["completed_slices", "truncated_slices"])
        self.stdout.write(f"Run #{run.pk}: {removed} slices will be collected again on resume.")

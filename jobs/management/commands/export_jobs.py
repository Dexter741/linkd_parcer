from django.core.management.base import BaseCommand, CommandError

from jobs.models import Job
from jobs.services.export import write_csv, write_xlsx


class Command(BaseCommand):
    help = "Export jobs with their company data to CSV or Excel (Excel also gets a Companies sheet)."

    def add_arguments(self, parser):
        parser.add_argument("output", help="File path; the format follows the extension (.csv or .xlsx).")
        parser.add_argument("--query", type=int, help="Only jobs found by this SearchQuery id.")
        parser.add_argument("--with-details", action="store_true", help="Only jobs whose details were fetched.")

    def handle(self, *args, **opts):
        jobs = Job.objects.order_by("-listed_at", "-id")
        if opts["query"]:
            jobs = jobs.filter(queries=opts["query"])
        if opts["with_details"]:
            jobs = jobs.filter(detail_status=Job.DetailStatus.DONE)

        path = opts["output"]
        if path.endswith(".csv"):
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                count = write_csv(jobs, f)
        elif path.endswith(".xlsx"):
            count = write_xlsx(jobs, path)
        else:
            raise CommandError("Output must end with .csv or .xlsx")
        self.stdout.write(f"Exported {count} jobs to {path}")

from django.core.management.base import BaseCommand

from jobs.linkedin.parsers import html_to_text
from jobs.models import Job

CHUNK = 500


class Command(BaseCommand):
    help = "Rebuild Job.description_text from the stored description HTML (after a parser change; no requests)."

    def handle(self, *args, **opts):
        jobs = Job.objects.exclude(description_html="").only("pk", "description_html", "description_text")
        changed, batch = 0, []
        for job in jobs.iterator(chunk_size=CHUNK):
            text = html_to_text(job.description_html)
            if text != job.description_text:
                job.description_text = text
                batch.append(job)
            if len(batch) >= CHUNK:
                changed += Job.objects.bulk_update(batch, ["description_text"])
                batch = []
        if batch:
            changed += Job.objects.bulk_update(batch, ["description_text"])
        self.stdout.write(f"Updated {changed} descriptions.")

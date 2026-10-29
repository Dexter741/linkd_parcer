"""CSV and Excel export of jobs together with their companies."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from typing import IO

from django.db.models import Count, Q, QuerySet
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter

from jobs.models import Company, Job

# (header, attribute path); "company.x" reads the related Company.
JOB_COLUMNS: list[tuple[str, str]] = [
    ("LinkedIn ID", "linkedin_id"),
    ("Title", "title"),
    ("Company", "company_name"),
    ("Location", "location"),
    ("Workplace type", "workplace_type"),
    ("Listed", "listed_at"),
    ("Posted", "posted_ago"),
    ("Salary", "salary"),
    ("Seniority", "seniority_level"),
    ("Employment type", "employment_type"),
    ("Job function", "job_function"),
    ("Industries", "industries"),
    ("Applicants", "applicants"),
    ("Closed", "is_closed"),
    ("Job URL", "url"),
    ("Apply URL", "apply_url"),
    ("Description", "description_text"),
    ("Company LinkedIn", "company.linkedin_url"),
    ("Company website", "company.website"),
    ("Company industry", "company.industry"),
    ("Company size", "company.company_size"),
    ("Company employees on LinkedIn", "company.employees_on_linkedin"),
    ("Company headquarters", "company.headquarters"),
    ("Company address", "company.address"),
    ("Company type", "company.company_type"),
    ("Company founded", "company.founded"),
    ("Company followers", "company.followers"),
    ("Company specialties", "company.specialties"),
    ("Company tagline", "company.tagline"),
    ("Company about", "company.description"),
    ("Job detail status", "detail_status"),
    ("Company detail status", "company.detail_status"),
    ("First seen", "first_seen_at"),
]

COMPANY_COLUMNS: list[tuple[str, str]] = [
    ("Name", "name"),
    ("LinkedIn", "linkedin_url"),
    ("Website", "website"),
    ("Industry", "industry"),
    ("Size", "company_size"),
    ("Employees on LinkedIn", "employees_on_linkedin"),
    ("Headquarters", "headquarters"),
    ("Address", "address"),
    ("Type", "company_type"),
    ("Founded", "founded"),
    ("Followers", "followers"),
    ("Specialties", "specialties"),
    ("Tagline", "tagline"),
    ("About", "description"),
    ("Jobs collected", "job_count"),
    ("Detail status", "detail_status"),
]

EXCEL_CELL_LIMIT = 32767
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
DISPLAY_CHOICES = {"workplace_type": Job.WorkplaceType}


def _neutralize_formula(value):
    """Scraped text starting with '=' etc. would run as a formula in Excel
    (CSV/formula injection). A leading apostrophe makes it plain text."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _value(obj, path: str):
    for part in path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part)
    if path in DISPLAY_CHOICES and obj is not None:
        obj = DISPLAY_CHOICES[path](obj).label if obj else ""
    if hasattr(obj, "tzinfo") and obj.tzinfo is not None:
        obj = obj.replace(tzinfo=None, microsecond=0)
    return _neutralize_formula(obj)


def _rows(records: Iterable, columns: list[tuple[str, str]]) -> Iterator[list]:
    paths = [path for _, path in columns]
    for record in records:
        yield [_value(record, path) for path in paths]


def job_rows(jobs: QuerySet[Job]) -> Iterator[list]:
    return _rows(jobs.select_related("company").iterator(chunk_size=500), JOB_COLUMNS)


def company_rows(jobs: QuerySet[Job]) -> Iterator[list]:
    ids = jobs.exclude(company=None).values("company_id")
    companies = Company.objects.filter(pk__in=ids).annotate(job_count=Count("jobs", filter=Q(jobs__in=jobs)))
    return _rows(companies.order_by("name").iterator(chunk_size=500), COMPANY_COLUMNS)


def csv_lines(jobs: QuerySet[Job]) -> Iterator[str]:
    """CSV as a stream of lines, for a streaming HTTP response."""

    class Echo:
        def write(self, value):
            return value

    writer = csv.writer(Echo())
    yield writer.writerow([h for h, _ in JOB_COLUMNS])
    for row in job_rows(jobs):
        yield writer.writerow(["" if v is None else v for v in row])


def write_csv(jobs: QuerySet[Job], out: IO[str]) -> int:
    """Write CSV to a text stream; returns the number of jobs. Callers should open
    files with encoding='utf-8-sig' so Excel detects UTF-8."""
    lines = csv_lines(jobs)
    out.write(next(lines))  # header
    count = 0
    for line in lines:
        out.write(line)
        count += 1
    return count


def _excel_safe(value):
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(value) > EXCEL_CELL_LIMIT:
            value = value[: EXCEL_CELL_LIMIT - 1] + "…"
    return value


def _sheet(wb: Workbook, title: str, columns: list[tuple[str, str]], rows: Iterator[list]) -> int:
    ws = wb.create_sheet(title)
    for i, (header, _) in enumerate(columns, start=1):
        wide = header in {"Title", "Name", "Description", "About", "Job URL", "Company LinkedIn", "LinkedIn"}
        ws.column_dimensions[get_column_letter(i)].width = 45 if wide else 18
    ws.freeze_panes = "A2"
    ws.append([h for h, _ in columns])
    count = 0
    for row in rows:
        ws.append([_excel_safe(v) for v in row])
        count += 1
    return count


def write_xlsx(jobs: QuerySet[Job], out: IO[bytes] | str) -> int:
    """Two sheets: Jobs (with company columns) and Companies. Returns the number of jobs."""
    wb = Workbook(write_only=True)
    count = _sheet(wb, "Jobs", JOB_COLUMNS, job_rows(jobs))
    _sheet(wb, "Companies", COMPANY_COLUMNS, company_rows(jobs))
    wb.save(out)
    return count

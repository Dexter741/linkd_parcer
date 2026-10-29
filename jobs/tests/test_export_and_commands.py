import csv
import io

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from openpyxl import load_workbook

from jobs.models import Company, Job, SearchQuery
from jobs.services.export import EXCEL_CELL_LIMIT, JOB_COLUMNS, write_csv, write_xlsx
from jobs.tests.conftest import FakeLinkedIn, make_jobs

pytestmark = pytest.mark.django_db


@pytest.fixture
def jobs():
    acme = Company.objects.create(
        linkedin_url="https://www.linkedin.com/company/acme/",
        slug="acme",
        name="Acme",
        industry="Software Development",
        company_size="51-200 employees",
        website="https://acme.example",
    )
    Job.objects.create(
        linkedin_id=1,
        url="https://www.linkedin.com/jobs/view/1/",
        title="Python Dev",
        company=acme,
        company_name="Acme",
        workplace_type=Job.WorkplaceType.REMOTE,
        description_text="ok",
        detail_status=Job.DetailStatus.DONE,
    )
    Job.objects.create(
        linkedin_id=2,
        url="https://www.linkedin.com/jobs/view/2/",
        title='=HYPERLINK("x")',
        company_name="Евро\x01Corp",
        description_text="x" * 40000,
    )
    return Job.objects.order_by("linkedin_id")


def column(name):
    return [h for h, _ in JOB_COLUMNS].index(name)


def test_csv_includes_company_data(jobs):
    out = io.StringIO()
    assert write_csv(jobs, out) == 2
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    assert rows[0] == [h for h, _ in JOB_COLUMNS]
    assert rows[1][column("Title")] == "Python Dev"
    assert rows[1][column("Workplace type")] == "Remote"
    assert rows[1][column("Company industry")] == "Software Development"
    assert rows[1][column("Company size")] == "51-200 employees"
    assert rows[1][column("Company website")] == "https://acme.example"
    assert rows[2][column("Title")] == '\'=HYPERLINK("x")'  # formula injection neutralised
    assert rows[2][column("Company industry")] == ""  # no company linked


def test_xlsx_has_jobs_and_companies_sheets(jobs):
    out = io.BytesIO()
    assert write_xlsx(jobs, out) == 2
    wb = load_workbook(io.BytesIO(out.getvalue()))
    rows = list(wb["Jobs"].values)
    assert rows[0][0] == "LinkedIn ID"
    assert rows[2][column("Title")] == '\'=HYPERLINK("x")'
    assert rows[2][column("Company")] == "ЕвроCorp"  # control char stripped
    assert len(rows[2][column("Description")]) == EXCEL_CELL_LIMIT
    companies = list(wb["Companies"].values)
    assert len(companies) == 2
    assert companies[1][:2] == ("Acme", "https://www.linkedin.com/company/acme/")
    assert companies[1][-2] == 1  # jobs collected


def test_export_command(jobs, tmp_path):
    csv_path = tmp_path / "jobs.csv"
    call_command("export_jobs", str(csv_path), "--with-details")
    content = csv_path.read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    assert len(list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))) == 2
    call_command("export_jobs", str(tmp_path / "jobs.xlsx"))
    assert (tmp_path / "jobs.xlsx").stat().st_size > 0


def test_admin_pages_and_export_actions(jobs, client):
    User.objects.create_superuser("admin", "a@example.com", "pw")
    client.login(username="admin", password="pw")
    query = SearchQuery.objects.create(keywords="Python Developer", country="United States")
    query.jobs.add(*jobs)
    company = Company.objects.get()
    for url in (
        "/admin/",
        "/admin/jobs/job/",
        "/admin/jobs/searchquery/",
        "/admin/jobs/searchquery/add/",
        f"/admin/jobs/searchquery/{query.pk}/change/",
        "/admin/jobs/searchrun/",
        "/admin/jobs/company/",
        f"/admin/jobs/company/{company.pk}/change/",
        f"/admin/jobs/job/{jobs[0].pk}/change/",
        "/admin/jobs/job/?q=python",
        f"/admin/jobs/job/?company__id__exact={company.pk}",
    ):
        assert client.get(url).status_code == 200, url

    resp = client.post("/admin/jobs/job/", {"action": "export_xlsx", "_selected_action": [j.pk for j in jobs]})
    assert resp.status_code == 200
    assert resp["Content-Disposition"].endswith('.xlsx"')
    resp = client.post("/admin/jobs/job/", {"action": "export_csv", "_selected_action": [jobs[0].pk]})
    body = b"".join(resp.streaming_content).decode("utf-8")
    assert body.startswith("﻿") and len(list(csv.reader(io.StringIO(body)))) == 2
    # Exporting a search query exports its jobs.
    resp = client.post("/admin/jobs/searchquery/", {"action": "export_csv", "_selected_action": [query.pk]})
    assert len(list(csv.reader(io.StringIO(b"".join(resp.streaming_content).decode("utf-8-sig"))))) == 3


def test_admin_rejects_duplicate_query(client):
    User.objects.create_superuser("admin", "a@example.com", "pw")
    client.login(username="admin", password="pw")
    SearchQuery.objects.create(keywords="Python Developer", country="USA")
    data = {"keywords": "python developer", "country": "usa", "is_active": "on", "runs-TOTAL_FORMS": "0",
            "runs-INITIAL_FORMS": "0", "runs-MIN_NUM_FORMS": "0", "runs-MAX_NUM_FORMS": "0"}  # fmt: skip
    resp = client.post("/admin/jobs/searchquery/add/", data)
    assert resp.status_code == 200
    assert "already exists" in resp.content.decode()
    assert SearchQuery.objects.count() == 1


def test_scrape_commands_end_to_end(monkeypatch, tmp_path):
    fake = FakeLinkedIn(make_jobs(25, companies=4))
    for module in ("scrape_search", "scrape_details"):
        monkeypatch.setattr(f"jobs.management.commands.{module}.LinkedInClient", lambda config: fake)

    out = io.StringIO()
    call_command("scrape_search", "--keywords", "Python Developer", "--country", "United States", stdout=out)
    assert "25 jobs (25 new)" in out.getvalue()
    call_command("scrape_details", "--limit", "10", stdout=out)
    assert "Jobs: 10 done" in out.getvalue()
    assert "Companies: 4 done" in out.getvalue()
    assert Job.objects.filter(detail_status=Job.DetailStatus.DONE).count() == 10

    call_command("export_jobs", str(tmp_path / "out.csv"), "--with-details", stdout=out)
    assert "Exported 10 jobs" in out.getvalue()


class FakeClock:
    """Replaces time.sleep/monotonic in a command module; sleeping advances the clock."""

    def __init__(self, monkeypatch, module, on_sleep=None):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = on_sleep
        monkeypatch.setattr(f"{module}.time.monotonic", lambda: self.now)
        monkeypatch.setattr(f"{module}.time.sleep", self.sleep)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))


def test_details_follow_waits_for_search_then_exits_when_idle(monkeypatch):
    fake = FakeLinkedIn({})
    module = "jobs.management.commands.scrape_details"
    monkeypatch.setattr(f"{module}.LinkedInClient", lambda config: fake)

    def search_adds_jobs(n_sleeps):
        if n_sleeps == 2:  # the search part stores more jobs while details is waiting
            for i in (101, 102):
                Job.objects.create(linkedin_id=i, url=f"https://www.linkedin.com/jobs/view/{i}/", title="t")

    clock = FakeClock(monkeypatch, "jobs.management.commands.scrape_details", search_adds_jobs)
    Job.objects.create(linkedin_id=100, url="https://www.linkedin.com/jobs/view/100/", title="t")
    out = io.StringIO()
    call_command("scrape_details", "--only", "jobs", "--follow", "--idle-minutes", "5", stdout=out)
    assert Job.objects.filter(detail_status=Job.DetailStatus.DONE).count() == 3
    assert "Queue empty for 5 min" in out.getvalue()
    assert clock.sleeps.count(60) >= 5


def test_details_wait_on_block_pauses_and_continues(monkeypatch):
    fake = FakeLinkedIn({})
    fake.throttle_after = 1
    monkeypatch.setattr("jobs.management.commands.scrape_details.LinkedInClient", lambda config: fake)

    def unblock(n_sleeps):
        fake.throttle_after = None

    clock = FakeClock(monkeypatch, "jobs.management.commands.scrape_details", unblock)
    for i in range(1, 4):
        Job.objects.create(linkedin_id=i, url=f"https://www.linkedin.com/jobs/view/{i}/", title="t")
    err = io.StringIO()
    call_command("scrape_details", "--only", "jobs", "--follow", "--idle-minutes", "1", "--wait-on-block", "15",
                 stdout=io.StringIO(), stderr=err)  # fmt: skip
    assert "pausing 15 min" in err.getvalue()
    assert 900 in clock.sleeps
    assert Job.objects.filter(detail_status=Job.DetailStatus.DONE).count() == 3


def test_search_wait_on_block_resumes_same_run(monkeypatch):
    from jobs.models import SearchRun

    fake = FakeLinkedIn(make_jobs(50))
    fake.throttle_after = 3
    monkeypatch.setattr("jobs.management.commands.scrape_search.LinkedInClient", lambda config: fake)

    def unblock(n_sleeps):
        fake.throttle_after = None

    clock = FakeClock(monkeypatch, "jobs.management.commands.scrape_search", unblock)
    call_command("scrape_search", "--keywords", "Python Developer", "--wait-on-block", "10",
                 stdout=io.StringIO(), stderr=io.StringIO())  # fmt: skip
    assert clock.sleeps == [600]
    assert SearchRun.objects.count() == 1
    assert SearchRun.objects.get().status == SearchRun.Status.DONE
    assert Job.objects.count() == 50


def test_follow_and_retry_failed_are_exclusive():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("scrape_details", "--follow", "--retry-failed")


def test_wait_on_block_without_follow_continues_after_pause(monkeypatch):
    # Seen live: the company run paused on throttling, then exited with the queue still full.
    fake = FakeLinkedIn({})
    fake.throttle_after = 2
    monkeypatch.setattr("jobs.management.commands.scrape_details.LinkedInClient", lambda config: fake)

    def unblock(n_sleeps):
        fake.throttle_after = None

    clock = FakeClock(monkeypatch, "jobs.management.commands.scrape_details", unblock)
    for i in range(1, 6):
        Company.objects.create(linkedin_url=f"https://www.linkedin.com/company/c{i}/", slug=f"c{i}")
    call_command("scrape_details", "--only", "companies", "--wait-on-block", "15",
                 stdout=io.StringIO(), stderr=io.StringIO())  # fmt: skip
    assert clock.sleeps == [900]
    assert Company.objects.filter(detail_status=Company.DetailStatus.DONE).count() == 5


def test_wait_on_block_respects_limit_across_pauses(monkeypatch):
    fake = FakeLinkedIn({})
    fake.throttle_after = 2
    monkeypatch.setattr("jobs.management.commands.scrape_details.LinkedInClient", lambda config: fake)

    def unblock(n_sleeps):
        fake.throttle_after = None

    FakeClock(monkeypatch, "jobs.management.commands.scrape_details", unblock)
    for i in range(1, 8):
        Job.objects.create(linkedin_id=i, url=f"https://www.linkedin.com/jobs/view/{i}/", title="t")
    call_command("scrape_details", "--only", "jobs", "--limit", "4", "--wait-on-block", "15",
                 stdout=io.StringIO(), stderr=io.StringIO())  # fmt: skip
    assert Job.objects.filter(detail_status=Job.DetailStatus.DONE).count() == 4

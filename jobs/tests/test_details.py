from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.linkedin.client import LinkedInError
from jobs.models import Company, Job
from jobs.services.details import claim_batch, run_details
from jobs.tests.conftest import FakeLinkedIn, fixture_html

pytestmark = pytest.mark.django_db
S = Job.DetailStatus


def make_job(linkedin_id, **kwargs):
    defaults = {"url": f"https://www.linkedin.com/jobs/view/{linkedin_id}/", "title": f"Job {linkedin_id}"}
    return Job.objects.create(linkedin_id=linkedin_id, **{**defaults, **kwargs})


def make_company(slug, **kwargs):
    return Company.objects.create(linkedin_url=f"https://www.linkedin.com/company/{slug}/", slug=slug, **kwargs)


def test_fetches_and_stores_job_details():
    make_job(1, company_name="", salary="")
    stats = run_details(FakeLinkedIn({}), Job)
    assert stats.done == 1
    job = Job.objects.get()
    assert job.detail_status == S.DONE
    assert job.seniority_level == "Mid-Senior level"
    assert job.employment_type == "Full-time"
    assert job.apply_url.startswith("https://careers.acme.example/")
    assert job.company_name == "Acme Corp"  # filled from the detail page
    assert job.salary.startswith("$140,000")
    assert job.title == "Job 1"  # existing card value kept
    assert job.detail_attempts == 1
    # A job whose card had no company link gets its company from the detail page.
    assert job.company.linkedin_url == "https://www.linkedin.com/company/acme-corp/"
    assert job.company.detail_status == S.PENDING


def test_fetches_and_stores_company_details():
    make_company("acme-corp", name="Acme")
    stats = run_details(FakeLinkedIn({}), Company)
    assert stats.done == 1
    company = Company.objects.get()
    assert company.detail_status == S.DONE
    assert company.name == "Acme Corp"
    assert company.industry == "Software Development"
    assert company.company_size == "1,001-5,000 employees"
    assert company.website == "https://www.acme.example/careers"
    assert company.followers == 12345


def test_company_login_wall_page_fails_without_wiping_data():
    make_company("acme", name="Acme", industry="Known")
    fake = FakeLinkedIn({})
    fake.company_responses = {"https://www.linkedin.com/company/acme/": "<h1>Join LinkedIn</h1>"}
    run_details(fake, Company)
    company = Company.objects.get()
    assert company.detail_status == S.FAILED
    assert company.name == "Acme" and company.industry == "Known"


def test_gone_failed_and_closed():
    for i in (1, 2, 3):
        make_job(i)
    fake = FakeLinkedIn({})
    fake.detail_responses = {1: None, 2: "<html>login wall</html>", 3: fixture_html("job_detail_closed.html")}
    stats = run_details(fake, Job)
    assert (stats.done, stats.gone, stats.failed) == (1, 1, 1)
    assert Job.objects.get(linkedin_id=1).detail_status == S.GONE
    failed = Job.objects.get(linkedin_id=2)
    assert failed.detail_status == S.FAILED and "ParseError" in failed.detail_error
    assert Job.objects.get(linkedin_id=3).is_closed is True


def test_unexpected_error_fails_only_that_record():
    make_job(1)
    make_job(2)
    fake = FakeLinkedIn({})
    fake.detail_responses = {1: RuntimeError("boom")}
    stats = run_details(fake, Job)
    assert (stats.done, stats.failed) == (1, 1)
    assert "RuntimeError: boom" in Job.objects.get(linkedin_id=1).detail_error


def test_over_long_values_are_clipped_not_crashing():
    make_job(1)
    fake = FakeLinkedIn({})
    long_function = "Engineering, " * 100
    fake.detail_responses = {
        1: fixture_html("job_detail.html").replace("Engineering and Information Technology", long_function)
    }
    assert run_details(fake, Job).done == 1
    assert len(Job.objects.get().job_function) == 500


def test_failed_records_retried_until_max_attempts(settings):
    settings.LINKEDIN = {**settings.LINKEDIN, "DETAIL_MAX_ATTEMPTS": 2}
    make_job(1)
    fake = FakeLinkedIn({})
    fake.detail_responses = {1: LinkedInError("HTTP 403")}
    run_details(fake, Job)
    run_details(fake, Job)
    assert Job.objects.get().detail_attempts == 2
    assert run_details(fake, Job).processed == 0  # exhausted
    assert run_details(fake, Job, retry_failed=True).processed == 1  # but can be forced, once per run


def test_rate_limit_releases_unprocessed_records():
    for i in range(1, 6):
        make_job(i)
    fake = FakeLinkedIn({})
    fake.throttle_after = 2
    stats = run_details(fake, Job, batch_size=5)
    assert stats.stopped_by_rate_limit and stats.done == 2
    assert Job.objects.filter(detail_status=S.DONE).count() == 2
    assert Job.objects.filter(detail_status=S.PENDING).count() == 3
    # The record that hit the throttling counts an attempt (goes to the back); the rest are untouched.
    assert sorted(Job.objects.filter(detail_status=S.PENDING).values_list("detail_attempts", flat=True)) == [0, 0, 1]


def test_record_that_always_hits_login_wall_does_not_block_queue(settings):
    from jobs.linkedin.client import RateLimited

    settings.LINKEDIN = {**settings.LINKEDIN, "DETAIL_MAX_ATTEMPTS": 3}
    for i in range(1, 4):
        make_job(i)
    fake = FakeLinkedIn({})
    fake.detail_responses = {1: RateLimited("redirected to the login wall")}
    run_details(fake, Job, batch_size=1)  # job 1 first, throttled, run stops
    stats = run_details(fake, Job, batch_size=1)  # next run starts with the others
    assert stats.done == 2
    for _ in range(3):
        run_details(fake, Job, batch_size=1)
    job = Job.objects.get(linkedin_id=1)
    assert job.detail_status == S.FAILED and job.detail_attempts == 3
    assert "throttled" in job.detail_error


def test_limit():
    for i in range(1, 6):
        make_job(i)
    assert run_details(FakeLinkedIn({}), Job, limit=3, batch_size=2).processed == 3
    assert Job.objects.filter(detail_status=S.PENDING).count() == 2


def test_claim_skips_records_claimed_by_another_worker_unless_stale():
    fresh = make_job(1, detail_status=S.IN_PROGRESS, detail_claimed_at=timezone.now())
    stale = make_job(2, detail_status=S.IN_PROGRESS, detail_claimed_at=timezone.now() - timedelta(hours=1))
    make_job(3, detail_status=S.DONE)
    claimed = claim_batch(Job, 10)
    assert [j.pk for j in claimed] == [stale.pk]
    assert fresh.pk not in [j.pk for j in claim_batch(Job, 10)]


def test_ctrl_c_releases_unprocessed_records():
    for i in range(1, 4):
        make_job(i)
    fake = FakeLinkedIn({})
    fake.detail_responses = {2: KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        run_details(fake, Job, batch_size=3)
    assert Job.objects.get(linkedin_id=1).detail_status == S.DONE
    assert set(Job.objects.filter(linkedin_id__in=[2, 3]).values_list("detail_status", flat=True)) == {S.PENDING}


def test_companies_of_done_jobs_only():
    from jobs.services.details import COMPANIES_OF_DONE_JOBS

    wanted, other = make_company("wanted"), make_company("other")
    make_job(1, company=wanted, detail_status=S.DONE)
    make_job(2, company=other)  # details not fetched: its company is skipped
    stats = run_details(FakeLinkedIn({}), Company, scope=COMPANIES_OF_DONE_JOBS)
    assert stats.done == 1
    assert Company.objects.get(slug="wanted").detail_status == S.DONE
    assert Company.objects.get(slug="other").detail_status == S.PENDING


def test_companies_with_most_jobs_are_fetched_first():
    small, big, medium = make_company("small"), make_company("big"), make_company("medium")
    for i, (company, n) in enumerate([(small, 1), (big, 5), (medium, 3)]):
        for j in range(n):
            make_job(i * 10 + j + 1, company=company)
    assert [c.slug for c in claim_batch(Company, 3)] == ["big", "medium", "small"]

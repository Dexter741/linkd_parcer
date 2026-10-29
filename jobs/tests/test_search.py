import pytest

from jobs.models import Company, Job, SearchQuery, SearchRun
from jobs.services.search import CAP_PROBE_START, SEARCH_LOCK_NAMESPACE, QueryBusy, run_search
from jobs.tests.conftest import COUNTRY, FakeLinkedIn, make_jobs

pytestmark = pytest.mark.django_db

REGIONS = "Texas, United States\nCalifornia, United States\nNew York, United States"


@pytest.fixture
def query():
    return SearchQuery.objects.create(keywords="Python Developer", country=COUNTRY, regions=REGIONS)


def locations(fake):
    return {p["location"] for p, _ in fake.search_calls}


def test_small_search_is_paginated_without_splitting(query):
    fake = FakeLinkedIn(make_jobs(35))
    run = run_search(query, fake)

    assert run.status == SearchRun.Status.DONE
    assert run.jobs_seen == run.jobs_new == 35
    assert Job.objects.count() == 35
    assert query.jobs.count() == 35
    assert run.slices == 1 and run.truncated_slices == 0
    # the probe and the end of the list are empty pages, each confirmed by a second request
    # page 0 first, then the probe (empty: confirmed by a second request), then the rest; the end is confirmed too
    assert [s for _, s in fake.search_calls] == [0, CAP_PROBE_START, CAP_PROBE_START, 10, 20, 30, 35, 35]
    assert all(p["sortBy"] == "DD" for p, _ in fake.search_calls)


def test_companies_are_created_once_and_linked(query):
    run_search(query, FakeLinkedIn(make_jobs(35, companies=7)))
    assert Company.objects.count() == 7
    company = Company.objects.get(slug="c1")
    assert company.linkedin_url == "https://www.linkedin.com/company/c1/"
    assert company.name == "Co c1"
    assert company.detail_status == Company.DetailStatus.PENDING
    assert company.jobs.count() == 5


def test_country_over_cap_is_split_by_region_first(query):
    fake = FakeLinkedIn(make_jobs(2400), cap=1000)
    run = run_search(query, fake)

    assert run.status == SearchRun.Status.DONE
    assert Job.objects.count() == 2400  # nothing lost to the cap
    # country, 3 regions of 800 each, then the country-level pass (for jobs listed with no region) by f_WT
    assert run.slices == 8
    assert run.truncated_slices == 0
    assert locations(fake) == {COUNTRY, *REGIONS.splitlines()}


def test_country_level_jobs_are_not_lost_when_splitting_by_region(query):
    # Postings listed as just "United States" (typical for remote jobs) are in no state's results.
    jobs = {**make_jobs(2400), **make_jobs(300, start_id=10_001, location=COUNTRY)}
    fake = FakeLinkedIn(jobs, cap=1000)
    run = run_search(query, fake)
    assert Job.objects.count() == 2700
    assert run.status == SearchRun.Status.DONE
    country_level = [p for p, _ in fake.search_calls if p["location"] == COUNTRY and "f_WT" in p]
    assert country_level  # the country-level search was collected split by facets


def test_region_over_cap_is_split_by_facets(query):
    fake = FakeLinkedIn(make_jobs(4500), cap=1000)  # 1500 per region
    run = run_search(query, fake)
    assert Job.objects.count() == 4500
    assert any("f_WT" in p for p, _ in fake.search_calls)
    assert run.truncated_slices == 0
    remote = Job.objects.filter(workplace_type=Job.WorkplaceType.REMOTE).count()
    assert remote == 1500  # learned from the f_WT=2 slice


def test_builtin_regions_used_when_none_given():
    query = SearchQuery.objects.create(keywords="Python", country="USA")
    assert len(query.region_list()) == 51
    assert query.region_list()[0] == "Alabama, United States"
    assert SearchQuery(keywords="x", country="Poland").region_list() == []


def test_geo_id_dropped_when_splitting_by_region(query):
    query.geo_id = "103644278"
    query.save()
    fake = FakeLinkedIn(make_jobs(2400), cap=1000)
    run_search(query, fake)
    region_calls = [p for p, _ in fake.search_calls if p["location"] != COUNTRY]
    assert region_calls and all("geoId" not in p for p in region_calls)


def test_unsplittable_search_is_reported_truncated():
    query = SearchQuery.objects.create(
        keywords="x", country="Texas, United States", work_types="2", job_types="F", experience_levels="4"
    )
    fake = FakeLinkedIn(make_jobs(1200, location="Texas, United States", f_WT="2", f_JT="F", f_E="4"))
    run = run_search(query, fake)
    assert run.truncated_slices == 1
    assert Job.objects.count() == 1000


def test_lossy_facet_split_also_paginates_parent():
    # Experience is the only facet left; a job without a level is still collected via the parent search.
    query = SearchQuery.objects.create(keywords="x", country="Texas, United States", work_types="2", job_types="F")
    jobs = make_jobs(1100, location="Texas, United States", f_WT="2", f_JT="F")
    jobs[1] = {**jobs[1], "f_E": ""}  # "Not applicable": matches no experience slice
    run = run_search(query, FakeLinkedIn(jobs))
    assert Job.objects.filter(linkedin_id=1).exists()
    # The fallback found a job the parts missed and hit the cap: more such jobs may lie beyond it.
    assert run.truncated_slices == 1


def test_lossy_fallback_that_finds_nothing_new_is_not_truncation():
    query = SearchQuery.objects.create(keywords="x", country="Texas, United States", work_types="2", job_types="F")
    run = run_search(query, FakeLinkedIn(make_jobs(1100, location="Texas, United States", f_WT="2", f_JT="F")))
    assert Job.objects.count() == 1100
    assert run.truncated_slices == 0


def test_lossy_fallback_reports_truncation_when_parent_is_capped():
    query = SearchQuery.objects.create(keywords="x", country="Texas, United States", work_types="2", job_types="F")
    jobs = make_jobs(2000, location="Texas, United States", f_WT="2", f_JT="F")
    for job_id in list(jobs)[:1000]:
        jobs[job_id] = {**jobs[job_id], "f_E": ""}  # half the jobs have no experience level
    run = run_search(query, FakeLinkedIn(jobs))
    assert run.truncated_slices == 1  # the loss is reported, not silent


def test_invalid_facet_codes_rejected():
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        SearchQuery(keywords="x", country="US", work_types="remote").full_clean()
    SearchQuery(keywords="x", country="US", work_types="1,2", job_types="F", experience_levels="4").full_clean()


def test_same_query_cannot_run_twice_at_once(query):
    import psycopg
    from django.db import connection

    s = connection.settings_dict
    other_process = psycopg.connect(
        dbname=s["NAME"], user=s["USER"], password=s["PASSWORD"], host=s["HOST"], port=s["PORT"], autocommit=True
    )
    with other_process:
        other_process.execute("SELECT pg_advisory_lock(%s, %s)", [SEARCH_LOCK_NAMESPACE, query.pk])
        with pytest.raises(QueryBusy):
            run_search(query, FakeLinkedIn(make_jobs(5)))
        other_process.execute("SELECT pg_advisory_unlock(%s, %s)", [SEARCH_LOCK_NAMESPACE, query.pk])
    assert run_search(query, FakeLinkedIn(make_jobs(5))).status == SearchRun.Status.DONE


def test_user_facet_filter_is_respected_and_used_for_splitting():
    query = SearchQuery.objects.create(keywords="x", country="Texas, United States", work_types="1,2")
    jobs = make_jobs(3000, location="Texas, United States")
    fake = FakeLinkedIn(jobs, cap=1000)
    run_search(query, fake)
    assert {p["f_WT"] for p, _ in fake.search_calls} == {"1,2", "1", "2"}
    assert Job.objects.count() == sum(1 for j in jobs.values() if j["f_WT"] in ("1", "2"))
    assert not Job.objects.filter(linkedin_id__in=[i for i, j in jobs.items() if j["f_WT"] == "3"]).exists()


def test_rerun_updates_existing_jobs_and_counts_nothing_new(query):
    run_search(query, FakeLinkedIn(make_jobs(15)))
    Job.objects.filter(linkedin_id=1).update(title="old", salary="$1")
    run = run_search(query, FakeLinkedIn(make_jobs(20)))
    assert run.jobs_seen == 20 and run.jobs_new == 5
    job = Job.objects.get(linkedin_id=1)
    assert job.title == "Python Developer 1"
    assert job.salary == "$1"  # an empty salary on the card does not wipe a known one


def test_rate_limit_stops_run_and_next_run_resumes(query):
    fake = FakeLinkedIn(make_jobs(2400), cap=1000)
    fake.throttle_after = 120  # Texas takes 83 requests; dies inside California
    first = run_search(query, fake)
    assert first.status == SearchRun.Status.BLOCKED
    assert "429" in first.error
    saved = Job.objects.count()
    assert 800 < saved < 2400
    assert len(first.completed_slices) == 1  # the first region

    fake.throttle_after = None
    fake.search_calls.clear()
    second = run_search(query, fake)
    assert second.pk == first.pk  # same run, continued
    assert second.status == SearchRun.Status.DONE
    assert Job.objects.count() == 2400
    assert "Texas, United States" not in locations(fake)  # the finished region is not fetched again
    assert second.requests_made > first.requests_made


def test_restart_ignores_unfinished_run(query):
    fake = FakeLinkedIn(make_jobs(50))
    fake.throttle_after = 2
    blocked = run_search(query, fake)
    fake.throttle_after = None
    fresh = run_search(query, fake, restart=True)
    assert fresh.pk != blocked.pk and fresh.status == SearchRun.Status.DONE


def test_max_pages(query):
    run = run_search(query, FakeLinkedIn(make_jobs(50)), max_pages=2)
    assert run.jobs_seen == 20


def test_job_shared_between_queries(query):
    other = SearchQuery.objects.create(keywords="Django", country=COUNTRY)
    run_search(query, FakeLinkedIn(make_jobs(5)))
    run_search(other, FakeLinkedIn(make_jobs(5)))
    assert Job.objects.count() == 5
    assert set(Job.objects.get(linkedin_id=1).queries.all()) == {query, other}


def test_duplicate_query_rejected(query):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        SearchQuery(keywords="python developer", country="united states").validate_constraints()


def test_result_cap_from_settings(query, settings):
    settings.LINKEDIN = {**settings.LINKEDIN, "RESULT_CAP": 400}
    fake = FakeLinkedIn(make_jobs(1200), cap=400)  # LinkedIn serves only 400 per search
    run = run_search(query, fake)
    assert run.status == SearchRun.Status.DONE
    assert Job.objects.count() == 1200  # 3 regions x 400 + country pass; nothing lost
    assert {s for _, s in fake.search_calls if s >= 300} >= {360}  # probed at 90 % of the cap


def test_find_cap_command(monkeypatch):
    import io

    from django.core.management import call_command

    fake = FakeLinkedIn(make_jobs(5000), cap=430)
    monkeypatch.setattr("jobs.management.commands.linkedin_probe.LinkedInClient", lambda config: fake)
    out = io.StringIO()
    call_command("linkedin_probe", "--find-cap", stdout=out)
    assert "LINKEDIN_RESULT_CAP=430" in out.getvalue()


def test_spurious_empty_pages_are_retried(query):
    fake = FakeLinkedIn(make_jobs(2400), cap=1000)
    fake.flaky_empty = {CAP_PROBE_START, 300}  # once: the saturation probe and a page mid-way
    run_search(query, fake)
    assert Job.objects.count() == 2400


def test_cold_deep_pages_do_not_hide_a_capped_search(query):
    # Seen live: a never-opened search answered start=900 with an empty page.
    fake = FakeLinkedIn(make_jobs(4500), cap=1000)  # 1500 per region
    fake.cold_deep_pages_empty = True
    run = run_search(query, fake)
    assert Job.objects.count() == 4500
    assert run.truncated_slices == 0


def test_probe_missing_the_cap_still_splits(query, monkeypatch):
    from jobs.services import search

    monkeypatch.setattr(search._SearchRunner, "_is_saturated", lambda self, params: False)  # probe always wrong
    fake = FakeLinkedIn(make_jobs(4500), cap=1000)
    run = run_search(query, fake)
    assert Job.objects.count() == 4500
    assert run.truncated_slices == 0


def test_forget_slices_command(query):
    import io

    from django.core.management import call_command

    run = SearchRun.objects.create(
        query=query,
        status=SearchRun.Status.RUNNING,
        truncated_slices=2,
        completed_slices=[
            "keywords=x&location=Alabama, United States&sortBy=DD",
            "keywords=x&location=Alaska, United States&sortBy=DD",
            "keywords=x&location=Georgia, United States&sortBy=DD",
        ],
    )
    out = io.StringIO()
    call_command("forget_slices", "Alabama, United States", "Georgia, United States", stdout=out)
    run.refresh_from_db()
    assert run.completed_slices == ["keywords=x&location=Alaska, United States&sortBy=DD"]
    assert "2 slices" in out.getvalue()

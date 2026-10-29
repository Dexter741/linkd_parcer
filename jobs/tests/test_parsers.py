from datetime import date

import pytest

from jobs.linkedin.parsers import (
    ParseError,
    company_page_url,
    parse_company_page,
    parse_job_detail,
    parse_search_page,
)
from jobs.tests.conftest import fixture_html


def test_search_page_cards():
    cards = parse_search_page(fixture_html("search_page.html"))
    assert [c.linkedin_id for c in cards] == [4012345678, 4012345679, 4012345680]

    first = cards[0]
    assert first.title == "Senior Python Developer"
    assert first.company_name == "Acme Corp"
    assert first.company_url == "https://www.linkedin.com/company/acme-corp/"
    assert first.location == "Austin, TX"
    assert first.salary == "$140,000.00 - $170,000.00"
    assert first.listed_at == date(2026, 10, 20)
    assert first.url == "https://www.linkedin.com/jobs/view/4012345678/"

    assert cards[1].listed_at == date(2026, 10, 27)  # the "--new" variant
    assert cards[1].salary == ""


def test_search_card_without_urn_uses_link_and_tolerates_bad_date():
    card = parse_search_page(fixture_html("search_page.html"))[2]
    assert card.linkedin_id == 4012345680
    assert card.company_name == "Initech"
    assert card.company_url == ""  # no company link on this card
    assert card.listed_at is None


@pytest.mark.parametrize("html", ["", "   ", "<html><body></body></html>"])
def test_empty_search_page(html):
    assert parse_search_page(html) == []


@pytest.mark.parametrize(
    "href, expected",
    [
        ("https://www.linkedin.com/company/acme-corp?trk=x", "https://www.linkedin.com/company/acme-corp/"),
        ("https://ua.linkedin.com/company/Acme-Corp/", "https://www.linkedin.com/company/acme-corp/"),
        ("https://www.linkedin.com/company/12345/life", "https://www.linkedin.com/company/12345/"),
        ("https://www.linkedin.com/showcase/acme-ai", "https://www.linkedin.com/showcase/acme-ai/"),
        ("https://www.linkedin.com/in/some-person", ""),
        ("https://evil.example/company/acme", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_company_page_url(href, expected):
    assert company_page_url(href) == expected


def test_job_detail():
    d = parse_job_detail(fixture_html("job_detail.html"))
    assert d.title == "Senior Python Developer"
    assert d.company_name == "Acme Corp"
    assert d.company_url == "https://www.linkedin.com/company/acme-corp/"
    assert d.location == "Austin, TX"
    assert d.posted_ago == "1 week ago"
    assert d.applicants == "Over 200 applicants"
    assert d.salary == "$140,000.00/yr - $170,000.00/yr"
    assert d.seniority_level == "Mid-Senior level"
    assert d.employment_type == "Full-time"
    assert d.job_function == "Engineering and Information Technology"
    assert d.industries == "Software Development"
    assert d.apply_url == "https://careers.acme.example/jobs/123?source=linkedin"
    assert "<li>5+ years of Python</li>" in d.description_html
    assert "Show more" not in d.description_text
    assert d.description_text == (
        "About the role\n\nWe build data pipelines in Python and Django.\n\n• 5+ years of Python\n• PostgreSQL"
    )
    assert d.is_closed is False


def test_closed_job_detail():
    d = parse_job_detail(fixture_html("job_detail_closed.html"))
    assert d.is_closed is True
    assert d.company_name == "Globex"
    assert d.description_text == "Short description."
    assert d.criteria == {}


def test_detail_parse_error_on_foreign_page():
    with pytest.raises(ParseError):
        parse_job_detail("<html><body><h1>Sign in</h1></body></html>")


def test_company_page():
    c = parse_company_page(fixture_html("company_page.html"))
    assert c.name == "Acme Corp"
    assert c.tagline == "Data tools for everyone"
    assert c.description == "Acme builds data tools for teams. We are hiring."
    assert c.website == "https://www.acme.example/careers"  # unwrapped from LinkedIn's redirect
    assert c.industry == "Software Development"
    assert c.company_size == "1,001-5,000 employees"
    assert c.headquarters == "Austin, TX"
    assert c.address == "500 W 2nd St, Austin, TX, 78701, US"
    assert c.company_type == "Privately Held"
    assert c.founded == "2009"
    assert c.specialties == "Python, Data Engineering, and Analytics"
    assert c.followers == 12345
    assert c.employees_on_linkedin == 1234
    assert c.logo_url == "https://media.licdn.com/dms/image/acme-logo-200.png"


def test_followers_not_taken_from_headquarters_number():
    # The fixture's subline is "Software Development Dublin 2, Dublin 12,345 followers" once rendered as text.
    assert parse_company_page(fixture_html("company_page.html")).followers == 12345


@pytest.mark.parametrize(
    "text, expected",
    [("1,234 followers", 1234), ("12K followers", 12000), ("1.5M followers", 1500000), ("no data", None)],
)
def test_followers_formats(text, expected):
    html = f'<h1 class="top-card-layout__title">X</h1><h3 class="top-card-layout__first-subline">{text}</h3>'
    assert parse_company_page(html).followers == expected


def test_apply_url_kept_when_not_wrapped():
    html = fixture_html("job_detail.html").replace(
        fixture_html("job_detail.html").split("<!--")[1].split("-->")[0], '"https://jobs.example/apply?a=1&amp;b=2"'
    )
    assert parse_job_detail(html).apply_url == "https://jobs.example/apply?a=1&b=2"


def test_company_page_falls_back_to_json_ld():
    c = parse_company_page(fixture_html("company_page_minimal.html"))
    assert c.name == "Globex"
    assert c.description == "Globex corp."
    assert c.website == "https://globex.example"
    assert c.logo_url == "https://media.licdn.com/globex.png"
    assert c.employees_on_linkedin == 42
    assert c.followers is None


def test_company_parse_error_on_login_wall():
    with pytest.raises(ParseError):
        parse_company_page("<html><body><h1>Join LinkedIn</h1></body></html>")


def test_html_to_text_keeps_inline_words_on_one_line():
    from jobs.linkedin.parsers import html_to_text

    html = (
        "<p>Experience using the <strong>Linux</strong> <em>CLI</em>, and <a href='#'>REST APIs</a>.</p>"
        "<p>Second&nbsp;paragraph<br>new line</p><ul><li>One</li><li><b>Two</b> items</li></ul>"
    )
    assert html_to_text(html) == (
        "Experience using the Linux CLI, and REST APIs.\n\nSecond paragraph\nnew line\n\n• One\n• Two items"
    )
    assert html_to_text("") == ""

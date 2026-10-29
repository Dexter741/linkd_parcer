"""Turn LinkedIn guest HTML into plain data.

Parsing is kept apart from HTTP so it can be tested on saved pages, and so a
markup change on LinkedIn's side means editing selectors in one place.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, Tag

JOB_URN_RE = re.compile(r"urn:li:jobPosting:(\d+)")
JOB_URL_ID_RE = re.compile(r"/jobs/view/(?:[^/?]*-)?(\d+)")
APPLY_URL_RE = re.compile(r'"(https?://[^"]+)"')
COMPANY_PATH_RE = re.compile(r"^/(company|showcase|school)/([^/?#]+)")
NUMBER_RE = re.compile(r"(\d[\d,.\s]*)\s*([KkMm])?")
FOLLOWERS_RE = re.compile(r"(\d[\d,.]*\s*[KkMm]?)\s+followers", re.IGNORECASE)


def job_url(job_id: int) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def company_page_url(href: str | None) -> str:
    """Canonical company page URL for any LinkedIn company link, or "" for anything else.

    Strips tracking parameters and country subdomains (ua.linkedin.com), so the
    same company always maps to the same URL; that URL is the dedup key.
    """
    parsed = urlparse((href or "").strip())
    if not parsed.netloc.endswith("linkedin.com"):
        return ""
    match = COMPANY_PATH_RE.match(parsed.path)
    if not match:
        return ""
    kind, slug = match.groups()
    return f"https://www.linkedin.com/{kind}/{unquote(slug).lower()}/"


def company_slug(url: str) -> str:
    match = COMPANY_PATH_RE.match(urlparse(url).path)
    return match.group(2) if match else ""


@dataclass
class JobCard:
    linkedin_id: int
    title: str
    company_name: str = ""
    company_url: str = ""
    location: str = ""
    listed_at: date | None = None
    salary: str = ""

    @property
    def url(self) -> str:
        return job_url(self.linkedin_id)


@dataclass
class JobDetail:
    title: str = ""
    company_name: str = ""
    company_url: str = ""
    location: str = ""
    posted_ago: str = ""
    applicants: str = ""
    salary: str = ""
    description_html: str = ""
    description_text: str = ""
    criteria: dict[str, str] = field(default_factory=dict)
    apply_url: str = ""
    is_closed: bool = False

    @property
    def seniority_level(self) -> str:
        return self.criteria.get("Seniority level", "")

    @property
    def employment_type(self) -> str:
        return self.criteria.get("Employment type", "")

    @property
    def job_function(self) -> str:
        return self.criteria.get("Job function", "")

    @property
    def industries(self) -> str:
        return self.criteria.get("Industries", "")


class ParseError(Exception):
    pass


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


BLOCK_TAGS = ["p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"]


def html_to_text(html: str) -> str:
    """Readable plain text: line breaks only where the HTML has blocks, line
    breaks or list items, never around inline tags like <strong> or <a>."""
    if not html.strip():
        return ""
    soup = _soup(html)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = [" ".join(line.split()) for line in soup.get_text().split("\n")]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _card_job_id(card: Tag) -> int | None:
    for node in [card, *card.find_all(attrs={"data-entity-urn": True})]:
        match = JOB_URN_RE.search(node.get("data-entity-urn", "") or "")
        if match:
            return int(match.group(1))
    link = card if card.name == "a" else card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
    if link:
        match = JOB_URL_ID_RE.search(link.get("href", ""))
        if match:
            return int(match.group(1))
    return None


def parse_search_page(html: str) -> list[JobCard]:
    """Cards from one `seeMoreJobPostings/search` response."""
    if not html.strip():
        return []
    soup = _soup(html)
    cards: list[JobCard] = []
    seen: set[int] = set()
    for card in soup.select(".base-search-card, .job-search-card"):
        job_id = _card_job_id(card)
        if job_id is None or job_id in seen:
            continue
        seen.add(job_id)
        company_link = card.select_one("h4.base-search-card__subtitle a")
        listed = card.select_one("time.job-search-card__listdate, time.job-search-card__listdate--new, time")
        cards.append(
            JobCard(
                linkedin_id=job_id,
                title=_text(card.select_one("h3.base-search-card__title")),
                company_name=_text(company_link or card.select_one("h4.base-search-card__subtitle")),
                company_url=company_page_url(company_link.get("href") if company_link else ""),
                location=_text(card.select_one("span.job-search-card__location")),
                listed_at=_parse_date(listed.get("datetime") if listed else None),
                salary=_text(card.select_one("span.job-search-card__salary-info")),
            )
        )
    return cards


def parse_job_detail(html: str) -> JobDetail:
    """Fields from one `jobPosting/<id>` response."""
    soup = _soup(html)
    title = _text(soup.select_one("h2.top-card-layout__title, h1.top-card-layout__title, h2.topcard__title"))
    # The inner markup div excludes the "Show more" button; the outer one is a fallback.
    description = soup.select_one("div.show-more-less-html__markup") or soup.select_one("div.description__text")
    if not title and description is None:
        raise ParseError("neither title nor description found; markup changed or page is not a job posting")

    company_link = soup.select_one("a.topcard__org-name-link")
    company = company_link or soup.select_one("span.topcard__flavor")

    criteria: dict[str, str] = {}
    for item in soup.select("li.description__job-criteria-item"):
        key = _text(item.select_one("h3.description__job-criteria-subheader"))
        value = _text(item.select_one("span.description__job-criteria-text"))
        if key:
            criteria[key] = value

    apply_url = ""
    apply_code = soup.select_one("code#applyUrl")
    if apply_code:
        # The external apply link sits inside an HTML comment in this <code> tag.
        match = APPLY_URL_RE.search(apply_code.decode_contents())
        if match:
            apply_url = _unwrap_redirect(html_lib.unescape(match.group(1)))

    description_html = description.decode_contents().strip() if description else ""
    description_text = html_to_text(description_html)

    return JobDetail(
        title=title,
        company_name=_text(company),
        company_url=company_page_url(company_link.get("href") if company_link else ""),
        location=_text(soup.select_one("span.topcard__flavor--bullet")),
        posted_ago=_text(soup.select_one("span.posted-time-ago__text")),
        applicants=_text(soup.select_one(".num-applicants__caption")),
        salary=_text(soup.select_one("div.salary.compensation__salary, div.compensation__salary")),
        description_html=description_html,
        description_text=description_text,
        criteria=criteria,
        apply_url=apply_url,
        is_closed=soup.select_one(".closed-job") is not None,
    )


@dataclass
class CompanyPage:
    name: str = ""
    tagline: str = ""
    description: str = ""
    website: str = ""
    industry: str = ""
    company_size: str = ""
    headquarters: str = ""
    address: str = ""
    company_type: str = ""
    founded: str = ""
    specialties: str = ""
    followers: int | None = None
    employees_on_linkedin: int | None = None
    logo_url: str = ""


def _parse_count(text: str) -> int | None:
    """'12,345 followers' -> 12345, '1.2K' -> 1200."""
    match = NUMBER_RE.search(text or "")
    if not match:
        return None
    digits, suffix = match.groups()
    digits = re.sub(r"[\s,]", "", digits)
    try:
        value = float(digits) if suffix else int(digits.replace(".", ""))
    except ValueError:
        return None
    multiplier = {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)
    return int(value * multiplier)


def _unwrap_redirect(href: str) -> str:
    """LinkedIn wraps outbound links as /redir/redirect?url=<real url> or
    /jobs/view/externalApply/<id>?url=<real url>; return the real URL."""
    href = (href or "").strip()
    parsed = urlparse(href)
    if parsed.netloc.endswith("linkedin.com") and (
        parsed.path.startswith("/redir/redirect") or parsed.path.startswith("/jobs/view/externalApply/")
    ):
        return parse_qs(parsed.query).get("url", [href])[0]
    return href


def _json_ld_organization(soup: BeautifulSoup) -> dict:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        candidates = data.get("@graph", [data]) if isinstance(data, dict) else data
        for item in candidates if isinstance(candidates, list) else []:
            if isinstance(item, dict) and item.get("@type") == "Organization":
                return item
    return {}


def parse_company_page(html: str) -> CompanyPage:
    """Fields from a public company page (linkedin.com/company/<slug>/).

    The visible "About us" block is read first; the page's JSON-LD
    Organization record fills whatever the HTML lacked.
    """
    soup = _soup(html)

    def about(key: str) -> str:
        return _text(soup.select_one(f'div[data-test-id="about-us__{key}"] dd'))

    website_link = soup.select_one('div[data-test-id="about-us__website"] dd a')
    logo = soup.select_one("img.top-card-layout__entity-image, img[data-ghost-classes*='entity-image']")
    employees_link = soup.select_one('a[data-tracking-control-name*="employees_all"], a.face-pile__cta')

    page = CompanyPage(
        name=_text(soup.select_one("h1.top-card-layout__title")),
        tagline=_text(soup.select_one("h4.top-card-layout__second-subline, h2.top-card-layout__headline")),
        description=_text(soup.select_one('p[data-test-id="about-us__description"]')),
        website=_unwrap_redirect(website_link.get("href", "") if website_link else "") or about("website"),
        industry=about("industry") or about("industries"),
        company_size=about("size"),
        headquarters=about("headquarters"),
        company_type=about("organizationType"),
        founded=about("foundedOn"),
        specialties=about("specialties"),
        logo_url=(logo.get("data-delayed-url") or logo.get("src") or "") if logo else "",
    )
    # "Software Development  Austin, TX  12,345 followers": the separators are drawn by CSS,
    # so take the number right before "followers", not the first number in the line.
    followers = FOLLOWERS_RE.search(_text(soup.select_one("h3.top-card-layout__first-subline")))
    if followers:
        page.followers = _parse_count(followers.group(1))
    if employees_link:
        page.employees_on_linkedin = _parse_count(_text(employees_link))

    org = _json_ld_organization(soup)
    if org:
        address = org.get("address") or {}
        if isinstance(address, dict):
            parts = [address.get(k, "") for k in ("streetAddress", "addressLocality", "addressRegion",
                                                  "postalCode", "addressCountry")]  # fmt: skip
            page.address = ", ".join(p for p in parts if isinstance(p, str) and p)
        logo_ld = org.get("logo")
        fallbacks = {
            "name": org.get("name"),
            "description": org.get("description"),
            "tagline": org.get("slogan"),
            "website": org.get("sameAs") if isinstance(org.get("sameAs"), str) else "",
            "logo_url": logo_ld.get("contentUrl") if isinstance(logo_ld, dict) else logo_ld,
        }
        for name, value in fallbacks.items():
            if not getattr(page, name) and isinstance(value, str):
                setattr(page, name, " ".join(value.split()) if name != "description" else value.strip())
        employees = org.get("numberOfEmployees")
        if page.employees_on_linkedin is None and isinstance(employees, dict):
            value = employees.get("value")
            page.employees_on_linkedin = value if isinstance(value, int) else None

    if not page.name:
        raise ParseError("company name not found; markup changed or page is not a company page")
    return page

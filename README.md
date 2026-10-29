# LinkedIn jobs parser

Collects LinkedIn job postings and their companies into PostgreSQL for a
search query (e.g. *Python Developer*) and a country (e.g. *USA*), and lets
you manage queries, browse results and export them (CSV / Excel) through the
Django admin.

Stack: Python 3.12+, Django 5.2, PostgreSQL 16, requests + BeautifulSoup/lxml, openpyxl, Docker.

## Quick start (Docker)

```bash
cp .env.example .env                 # set DJANGO_SECRET_KEY, POSTGRES_PASSWORD, DJANGO_ALLOWED_HOSTS
docker compose up -d --build         # PostgreSQL + admin on http://localhost:8000/admin/
docker compose run --rm app python manage.py createsuperuser

# add a search query in the admin (Search queries -> Add), or:
docker compose run --rm app python manage.py shell -c \
  "from jobs.models import SearchQuery; SearchQuery.objects.get_or_create(keywords='Python Developer', country='United States')"

docker compose --profile scrape up -d    # Search, then Details, in the background
docker compose logs -f scrape

docker compose run --rm app python manage.py export_jobs output/jobs.xlsx --with-details   # -> ./output/
```

## Results of the test run

*Python Developer* / *United States*, one office IP, default pacing (2–5 s per request):

| | |
|---|---|
| Jobs found by Search | **26 090** (51 states; saturated states split by workplace type) |
| Jobs with full details | **13 072** (Details stopped at the 10k target; 8 removed by LinkedIn, 0 parse errors) |
| Companies linked to those jobs | ~5 700; name, LinkedIn URL and industry for every job — full profiles see *Known limitations* |
| Export | `linkedin_jobs.xlsx` (Jobs + Companies sheets), `linkedin_jobs.csv` |

Two things the live run revealed and the code now handles: LinkedIn answers
deep pages of a search that was never opened from page 0 with an empty page,
and it occasionally returns a spurious empty page; both made capped searches
look small until page 0 was fetched first and empty pages were confirmed.

## How it works

Two independent parts share only the database:

| Part | Command | Reads | Writes |
|---|---|---|---|
| **Search** | `scrape_search` | `SearchQuery` (keywords + country) | `Job` cards (id, title, company, location, date, salary, workplace type) and `Company` stubs |
| **Details** | `scrape_details` | `Job` / `Company` rows with `detail_status = pending` | full job posting; full company profile |

Either can run without the other, at any time, on any machine with access
to the database. Details picks up whatever Search has stored so far.

### Data sources (found in DevTools → Network)

Only LinkedIn's public, logged-out pages are used — no account, no cookies.
Open `linkedin.com/jobs/search?keywords=Python%20Developer&location=United%20States`
logged out, scroll, and watch DevTools → Network:

| What | Request | Returns |
|---|---|---|
| Search results | `GET /jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=…&location=…&sortBy=DD&start=N` (+ `f_WT`, `f_JT`, `f_E`, `f_TPR`, `geoId`) | HTML, ~10 job cards per page |
| Job posting | `GET /jobs-guest/jobs/api/jobPosting/<job id>` (requested when you click a card) | HTML fragment with the full posting |
| Company | `GET /company/<slug>/` (the link on the posting) | public company page, with an "About us" block and a JSON-LD `Organization` record |

The authenticated JSON API (Voyager) is deliberately not used: scraping with
an account breaks LinkedIn's User Agreement and gets the account restricted.

### What is collected

- **Job:** LinkedIn id and URL, title, company, location, listing date,
  "posted … ago", salary (when published), workplace type (on-site / remote /
  hybrid, when known from the search filter), seniority level, employment type,
  job function, industries, number of applicants, external apply URL,
  closed-for-applications flag, full description (HTML and text).
- **Company:** name, LinkedIn URL, website, tagline, about text, industry,
  company size, employees on LinkedIn, headquarters, full address, company
  type, year founded, specialties, followers, logo URL.

### All jobs, not just the first 1000

LinkedIn returns at most ~1000 results for one search. Before paginating a
search the Search part requests the page at `start=900` (a margin below the
cap). If it has results, the search is saturated and is split, recursively:

1. **by region** of the country — built in for the USA (51 states), Canada,
   the UK and Germany; for any other country list regions in the query's
   *Regions* field. Postings listed for the whole country ("United States",
   typical for remote jobs) are in no region, so the country-level search is
   then collected again, split by facets only;
2. **by workplace type, then job type** — every job has exactly one value of
   each, so the parts together cover the same jobs;
3. **by experience level** as a last resort; jobs with no level fall into no
   part, so the unsplit search is paginated too.

A slice that still reaches the cap with nothing left to split by is counted in
`SearchRun.truncated_slices`, so possible data loss is visible, never silent. Results are sorted by date
(`sortBy=DD`); the default relevance order reshuffles between page requests
and makes pagination skip and repeat jobs.

### No duplicates

- Jobs are unique by LinkedIn id, companies by canonical company URL
  (tracking parameters and country subdomains stripped). Re-running updates
  rows instead of inserting new ones, and an empty value from a sparser page
  never overwrites a known one.
- A job found by several queries is stored once and linked to all of them.
- Search queries are unique by keywords + country + filters, case-insensitive.
- Inserts use `ON CONFLICT DO NOTHING`, so parallel runs cannot create duplicates either.

### Errors and re-runs

- A random 2–5 s pause before every request. HTTP 429, LinkedIn's 999 and
  redirects to the login wall trigger exponential backoff (30 s, 60 s, 120 s, …;
  `Retry-After` is honoured). If throttling persists the run stops cleanly.
- **Search** records every finished slice on its `SearchRun`. The next
  `scrape_search` resumes an unfinished run where it stopped (`--restart` to
  start over). A PostgreSQL advisory lock keeps two processes from running the
  same query at once; the second one skips it.
- **Details** treats the tables as a queue and claims batches with
  `SELECT … FOR UPDATE SKIP LOCKED`: several workers (e.g. one per proxy) never
  take the same record, and records claimed by a crashed worker are released
  after 30 minutes. A 404 marks the record *gone*; any other error marks only
  that record *failed* with the reason, and it is retried on the next runs up
  to 3 attempts (`--retry-failed` forces more). On throttling the run stops,
  unprocessed records go back to the queue, and the record that hit the
  throttling moves to the back of the queue, so a page that always shows the
  login wall cannot block the rest. Ctrl+C returns the claimed batch at once.
- Scraped values longer than their column are truncated instead of failing a batch.

Run collection from a machine whose IP you can afford to have rate-limited —
not from an office network other people depend on.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # adjust if needed
docker compose up -d             # PostgreSQL; or point .env at an existing one
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver       # admin at http://127.0.0.1:8000/admin/
```

## Usage

```bash
# 0. Check LinkedIn is reachable and the selectors match (saves nothing to the DB)
python manage.py linkedin_probe --keywords "Python Developer" --country "United States" --save-html probe_html

# 1. Add search queries in the admin (Search queries → Add), or ad hoc:
python manage.py scrape_search --keywords "Python Developer" --country "United States"

# 2. Search part: all active queries; re-run to resume after a stop
python manage.py scrape_search --all

# 3. Details part: jobs, then companies; re-run any time, run several in parallel if you like
python manage.py scrape_details
python manage.py scrape_details --only companies --limit 200

# 4. Export
python manage.py export_jobs jobs.xlsx                 # sheets: Jobs (with company columns), Companies
python manage.py export_jobs jobs.csv --with-details   # one row per job, company columns included
```

### Admin

- **Search queries** — add and edit queries (keywords, country, optional
  filters and regions), switch them on/off, see each query's job count, last
  run status and run history; export a query's jobs.
- **Jobs** — all collected jobs with filters (processing status, closed,
  workplace type, employment type, seniority, date, query), search, full
  details, processing status/attempts/error; export selected jobs; re-queue
  details.
- **Companies** — company profiles, their jobs, processing status.
- **Search runs** — requests made, slices, truncation, errors for every run.

Exports: CSV is UTF-8 with BOM so Excel opens it correctly. Values that start
with `=`, `+`, `-`, `@` are prefixed with `'` (no formula injection from
scraped text). Excel cells are capped at Excel's 32 767-character limit.

## Running on several machines

Every machine with a different IP adds throughput. All of them use one
PostgreSQL; the others reach it through an SSH tunnel, so the database port is
never exposed:

```bash
# on the database machine (A): PostgreSQL listens on localhost only
python manage.py migrate
python manage.py scrape_search --all --wait-on-block 15

# on every other machine (B, C, …)
ssh -N -L 5433:localhost:5432 user@machine-a &     # tunnel to A's PostgreSQL
echo "POSTGRES_PORT=5433" >> .env
python manage.py scrape_details --follow --wait-on-block 15
```

`--follow` keeps a details worker waiting for new records while the search is
still running (it exits after 30 idle minutes); `--wait-on-block` pauses on
throttling and continues instead of stopping. Workers never take the same
record (`SKIP LOCKED`), and two machines never run the same search query.
Check each machine with `linkedin_probe` first: datacenter IPs get blocked by
LinkedIn much sooner than home or mobile ones.

## Volume and timing

A request takes ~3.5 s on average with the default pacing. For 5 000 jobs:
~550 search requests, 5 000 job pages and one page per distinct company
(typically 30–50 % of the job count) — about 7 hours from one IP. Run
`scrape_details` workers behind different proxies (`LINKEDIN_PROXY`) to divide
that; the search part can run at the same time.

## Tests

```bash
pytest
```

Tests need PostgreSQL (the queue uses `SKIP LOCKED`). They never touch the
network: parsers run on saved HTML fixtures, and the stages run against an
in-memory fake of LinkedIn that reproduces pagination and the 1000-result cap,
so splitting, resuming and deduplication are tested on up to 4 500 synthetic jobs.

## Project layout

```
config/                     Django settings (all configuration via env vars)
jobs/models.py              SearchQuery, SearchRun, Job, Company
jobs/linkedin/client.py     HTTP: pacing, retries, backoff, login-wall detection, proxy
jobs/linkedin/parsers.py    HTML → dataclasses (all CSS selectors live here)
jobs/linkedin/regions.py    regions used to split a country-wide search
jobs/services/search.py     Search part: splitting, resume, upserts
jobs/services/details.py    Details part: SKIP LOCKED queue for jobs and companies
jobs/services/export.py     CSV / Excel
jobs/management/commands/   scrape_search, scrape_details, export_jobs, linkedin_probe
jobs/admin.py               admin sections and export actions
```

## Known limitations

- LinkedIn changes its markup from time to time. All selectors are in
  `jobs/linkedin/parsers.py`; `linkedin_probe --save-html` shows what broke,
  and a failed parse marks the record `failed` with the reason instead of
  storing empty data.
- **Company pages are effectively closed to logged-out visitors at scale.**
  Measured during the 26k-job run: job pages were served without limit, but
  LinkedIn shows a guest one company page and then the login wall (HTTP 999 or
  a redirect to /authwall), from any IP and in a real browser too. Full
  company profiles were therefore collected only for a few companies. Every
  job still carries its company's name, LinkedIn URL and industry (from the
  posting's "Industries" criterion). Companies are queued most-jobs-first, so
  whatever does get through covers as many jobs as possible; with a rotating
  residential proxy (`LINKEDIN_PROXY`, a new IP per request) the company stage
  can run in full. Logging in was ruled out: scraping with an account breaks
  LinkedIn's User Agreement and gets the account restricted.
- `listed_at` is a date (cards carry only a date); `posted_ago` is the relative
  text from the posting.
- Workplace type is known only for jobs found by a search split (or filtered)
  by workplace type; LinkedIn's logged-out posting page does not show it.

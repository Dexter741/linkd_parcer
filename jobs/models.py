from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower

from jobs.linkedin.regions import regions_for


def _codes_validator(codes: str, label: str) -> RegexValidator:
    return RegexValidator(
        rf"^[{codes}](,[{codes}])*$", f"Comma-separated {label} codes from: {', '.join(codes)} (e.g. '{codes[0]}')."
    )


class SearchQuery(models.Model):
    """A saved LinkedIn job search: keywords + country. The search stage runs these."""

    class TimePosted(models.TextChoices):
        ANY = "", "Any time"
        DAY = "r86400", "Past 24 hours"
        WEEK = "r604800", "Past week"
        MONTH = "r2592000", "Past month"

    name = models.CharField(max_length=200, blank=True)
    keywords = models.CharField(max_length=300, help_text="E.g. Python Developer.")
    country = models.CharField(
        max_length=200,
        help_text="Country as typed in LinkedIn's location box, e.g. 'United States'.",
    )
    regions = models.TextField(
        blank=True,
        help_text=(
            "Optional. One LinkedIn location per line (e.g. 'Texas, United States'). When the country-wide search "
            "exceeds LinkedIn's ~1000 results cap it is split into these regions. Leave empty to use the built-in "
            "list for the United States, Canada, the United Kingdom or Germany."
        ),
    )
    geo_id = models.CharField(
        max_length=32, blank=True, help_text="Optional LinkedIn geoId; more precise than the country text."
    )
    time_posted = models.CharField(max_length=16, choices=TimePosted.choices, blank=True, default="")
    # Comma-separated LinkedIn facet codes. Empty means "do not filter"; the
    # search stage may still split by these facets to get past the result cap.
    work_types = models.CharField(
        max_length=20,
        blank=True,
        validators=[_codes_validator("123", "workplace type")],
        help_text="f_WT: 1 on-site, 2 remote, 3 hybrid.",
    )
    job_types = models.CharField(
        max_length=30,
        blank=True,
        validators=[_codes_validator("FPCTIVO", "job type")],
        help_text="f_JT: F full-time, P part-time, C contract, T temporary, I internship, V volunteer, O other.",
    )
    experience_levels = models.CharField(
        max_length=20,
        blank=True,
        validators=[_codes_validator("123456", "experience level")],
        help_text="f_E: 1 internship, 2 entry, 3 associate, 4 mid-senior, 5 director, 6 executive.",
    )
    is_active = models.BooleanField(default=True, help_text="Included in `scrape_search --all`.")
    created_at = models.DateTimeField(auto_now_add=True)
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "search queries"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                Lower("keywords"),
                Lower("country"),
                "time_posted",
                "work_types",
                "job_types",
                "experience_levels",
                name="unique_search_query",
                violation_error_message="This search query already exists.",
            )
        ]

    def __str__(self):
        return self.name or f"{self.keywords} — {self.country}"

    def base_params(self) -> dict[str, str]:
        # sortBy=DD (newest first) keeps result order stable while we paginate;
        # the default relevance order reshuffles between requests.
        params = {"keywords": self.keywords, "location": self.country, "sortBy": "DD"}
        optional = {
            "geoId": self.geo_id,
            "f_TPR": self.time_posted,
            "f_WT": self.work_types,
            "f_JT": self.job_types,
            "f_E": self.experience_levels,
        }
        params.update({k: v.replace(" ", "") for k, v in optional.items() if v})
        return params

    def region_list(self) -> list[str]:
        custom = [line.strip() for line in self.regions.splitlines() if line.strip()]
        return custom or regions_for(self.country)


class SearchRun(models.Model):
    """One execution of a SearchQuery. An unfinished run is resumed by the next `scrape_search`."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        BLOCKED = "blocked", "Stopped: rate limited"
        FAILED = "failed", "Failed"

    query = models.ForeignKey(SearchQuery, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    requests_made = models.PositiveIntegerField(default=0)
    slices = models.PositiveIntegerField(default=0, help_text="Sub-searches the query was split into.")
    truncated_slices = models.PositiveIntegerField(
        default=0, help_text="Slices that still hit LinkedIn's result cap and could not be split further."
    )
    completed_slices = models.JSONField(
        default=list, blank=True, help_text="Slices already collected; skipped when the run is resumed."
    )
    jobs_seen = models.PositiveIntegerField(default=0)
    jobs_new = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Run #{self.pk} of {self.query}"


class DetailQueue(models.Model):
    """Fields that turn a table into a work queue for the detail stage."""

    class DetailStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        GONE = "gone", "Removed from LinkedIn"

    detail_status = models.CharField(
        max_length=16, choices=DetailStatus.choices, default=DetailStatus.PENDING, db_index=True
    )
    detail_attempts = models.PositiveSmallIntegerField(default=0)
    detail_error = models.TextField(blank=True)
    detail_claimed_at = models.DateTimeField(null=True, blank=True)
    detail_fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Company(DetailQueue):
    """A company page on LinkedIn. Created by the search stage, filled by the detail stage."""

    linkedin_url = models.URLField(max_length=300, unique=True)
    slug = models.CharField(max_length=200)
    name = models.CharField(max_length=300, blank=True)
    tagline = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    website = models.URLField(max_length=500, blank=True)
    industry = models.CharField(max_length=200, blank=True)
    company_size = models.CharField(max_length=100, blank=True)
    headquarters = models.CharField(max_length=300, blank=True)
    address = models.CharField(max_length=500, blank=True)
    company_type = models.CharField(max_length=100, blank=True)
    founded = models.CharField(max_length=20, blank=True)
    specialties = models.TextField(blank=True)
    followers = models.PositiveIntegerField(null=True, blank=True)
    employees_on_linkedin = models.PositiveIntegerField(null=True, blank=True)
    logo_url = models.URLField(max_length=1000, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]
        indexes = [models.Index(fields=["detail_status", "first_seen_at"])]

    def __str__(self):
        return self.name or self.slug


class Job(DetailQueue):
    """A LinkedIn job posting. Card fields come from search, the rest from the detail stage."""

    class WorkplaceType(models.TextChoices):
        UNKNOWN = "", "Unknown"
        ON_SITE = "1", "On-site"
        REMOTE = "2", "Remote"
        HYBRID = "3", "Hybrid"

    linkedin_id = models.BigIntegerField(unique=True)
    url = models.URLField(max_length=300)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs")

    # From the search card.
    title = models.CharField(max_length=500)
    company_name = models.CharField(max_length=300, blank=True)
    company_url = models.URLField(max_length=500, blank=True)
    location = models.CharField(max_length=300, blank=True)
    listed_at = models.DateField(null=True, blank=True)
    salary = models.CharField(max_length=200, blank=True)
    workplace_type = models.CharField(
        max_length=1,
        choices=WorkplaceType.choices,
        blank=True,
        help_text="Known when the job was found by a search filtered on workplace type.",
    )
    queries = models.ManyToManyField(SearchQuery, related_name="jobs", blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    # From the detail page.
    description_html = models.TextField(blank=True)
    description_text = models.TextField(blank=True)
    seniority_level = models.CharField(max_length=100, blank=True)
    employment_type = models.CharField(max_length=100, blank=True)
    job_function = models.CharField(max_length=500, blank=True)
    industries = models.CharField(max_length=500, blank=True)
    applicants = models.CharField(max_length=100, blank=True)
    posted_ago = models.CharField(max_length=100, blank=True)
    apply_url = models.URLField(max_length=1000, blank=True)
    is_closed = models.BooleanField(default=False, help_text="LinkedIn says it no longer accepts applications.")

    class Meta:
        ordering = ["-listed_at", "-id"]
        indexes = [models.Index(fields=["detail_status", "first_seen_at"])]

    def __str__(self):
        return f"{self.title} — {self.company_name}"


def clip_to_fields(instance: models.Model) -> None:
    """Truncate CharField values to their max_length.

    Scraped text has no length guarantee, and one over-long value would make
    PostgreSQL reject the whole batch insert.
    """
    for field in instance._meta.concrete_fields:
        if isinstance(field, models.CharField) and field.max_length:
            value = getattr(instance, field.attname)
            if isinstance(value, str) and len(value) > field.max_length:
                setattr(instance, field.attname, value[: field.max_length])

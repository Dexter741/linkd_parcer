import io

from django import forms
from django.contrib import admin
from django.db import models
from django.db.models import Count, OuterRef, Subquery
from django.http import HttpResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from jobs.models import Company, Job, SearchQuery, SearchRun
from jobs.services.export import csv_lines, write_xlsx

admin.site.site_header = "LinkedIn jobs parser"
admin.site.site_title = "LinkedIn jobs parser"
admin.site.index_title = "Search queries, jobs and companies"


def _filename(ext: str) -> str:
    return f"linkedin_jobs_{timezone.now():%Y%m%d_%H%M}.{ext}"


def _jobs_of(queryset):
    """The jobs behind an admin selection: the jobs themselves, or those of selected queries/companies."""
    if queryset.model is Job:
        return queryset
    if queryset.model is SearchQuery:
        return Job.objects.filter(queries__in=queryset).distinct()
    return Job.objects.filter(company__in=queryset)


@admin.action(description="Export jobs to CSV")
def export_csv(modeladmin, request, queryset):
    lines = csv_lines(_jobs_of(queryset).order_by("-listed_at", "-id"))
    response = StreamingHttpResponse(
        ("﻿" + line if i == 0 else line for i, line in enumerate(lines)),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{_filename("csv")}"'
    return response


@admin.action(description="Export jobs to Excel (with a Companies sheet)")
def export_xlsx(modeladmin, request, queryset):
    buffer = io.BytesIO()
    write_xlsx(_jobs_of(queryset).order_by("-listed_at", "-id"), buffer)
    response = HttpResponse(
        buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{_filename("xlsx")}"'
    return response


@admin.action(description="Queue details to be fetched again")
def requeue_details(modeladmin, request, queryset):
    updated = queryset.update(detail_status=Job.DetailStatus.PENDING, detail_attempts=0, detail_claimed_at=None)
    modeladmin.message_user(request, f"{updated} records queued for the detail stage.")


class HttpsURLFields:
    formfield_overrides = {models.URLField: {"form_class": forms.URLField, "assume_scheme": "https"}}


def _open_link(url: str, label: str = "open"):
    if not url:
        return "—"
    return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', url, label)


# --- Search queries ---------------------------------------------------------


class SearchRunInline(admin.TabularInline):
    model = SearchRun
    extra = 0
    can_delete = False
    fields = (
        "status",
        "started_at",
        "finished_at",
        "jobs_seen",
        "jobs_new",
        "slices",
        "truncated_slices",
        "requests_made",
        "error",
    )
    readonly_fields = fields
    show_change_link = True
    max_num = 0


@admin.register(SearchQuery)
class SearchQueryAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "keywords",
        "country",
        "time_posted",
        "is_active",
        "job_count",
        "last_status",
        "last_run_at",
    )
    list_filter = ("is_active", "country", "time_posted")
    list_editable = ("is_active",)
    search_fields = ("name", "keywords", "country")
    actions = [export_csv, export_xlsx]
    inlines = [SearchRunInline]
    fieldsets = (
        (None, {"fields": ("name", "keywords", "country", "is_active")}),
        ("Filters", {"fields": ("time_posted", "work_types", "job_types", "experience_levels")}),
        ("Advanced", {"classes": ("collapse",), "fields": ("regions", "geo_id")}),
    )

    def get_queryset(self, request):
        last_run = SearchRun.objects.filter(query=OuterRef("pk")).order_by("-started_at")
        return (
            super()
            .get_queryset(request)
            .annotate(_job_count=Count("jobs", distinct=True), _last_status=Subquery(last_run.values("status")[:1]))
        )

    @admin.display(description="Jobs", ordering="_job_count")
    def job_count(self, obj):
        return obj._job_count

    @admin.display(description="Last run")
    def last_status(self, obj):
        return SearchRun.Status(obj._last_status).label if obj._last_status else "never run"


@admin.register(SearchRun)
class SearchRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "query",
        "status",
        "started_at",
        "finished_at",
        "jobs_seen",
        "jobs_new",
        "slices",
        "truncated_slices",
        "requests_made",
    )
    list_filter = ("status", "query")
    readonly_fields = [f.name for f in SearchRun._meta.fields]

    def has_add_permission(self, request):
        return False


# --- Jobs -------------------------------------------------------------------


@admin.register(Job)
class JobAdmin(HttpsURLFields, admin.ModelAdmin):
    list_display = (
        "title",
        "company_link",
        "location",
        "workplace_type",
        "listed_at",
        "salary",
        "employment_type",
        "detail_status",
        "is_closed",
        "linkedin_link",
    )
    list_filter = (
        "detail_status",
        "is_closed",
        "workplace_type",
        "employment_type",
        "seniority_level",
        "listed_at",
        "queries",
    )
    list_select_related = ("company",)
    search_fields = ("title", "company_name", "location", "=linkedin_id")
    date_hierarchy = "listed_at"
    list_per_page = 50
    actions = [export_csv, export_xlsx, requeue_details]
    raw_id_fields = ("company",)
    readonly_fields = ("first_seen_at", "last_seen_at", "detail_fetched_at", "detail_claimed_at")
    exclude = ("description_html",)
    filter_horizontal = ("queries",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "linkedin_id",
                    "url",
                    "title",
                    "company",
                    "company_name",
                    "location",
                    "workplace_type",
                    "listed_at",
                    "salary",
                )
            },
        ),
        (
            "Details",
            {
                "fields": (
                    "seniority_level",
                    "employment_type",
                    "job_function",
                    "industries",
                    "applicants",
                    "posted_ago",
                    "apply_url",
                    "is_closed",
                    "description_text",
                )
            },
        ),
        (
            "Processing",
            {
                "fields": (
                    "detail_status",
                    "detail_attempts",
                    "detail_error",
                    "detail_claimed_at",
                    "detail_fetched_at",
                    "queries",
                    "first_seen_at",
                    "last_seen_at",
                ),
            },
        ),
    )

    @admin.display(description="LinkedIn")
    def linkedin_link(self, obj):
        return _open_link(obj.url)

    @admin.display(description="Company", ordering="company_name")
    def company_link(self, obj):
        if obj.company_id is None:
            return obj.company_name
        url = reverse("admin:jobs_company_change", args=[obj.company_id])
        return format_html('<a href="{}">{}</a>', url, obj.company_name)


# --- Companies --------------------------------------------------------------


@admin.register(Company)
class CompanyAdmin(HttpsURLFields, admin.ModelAdmin):
    list_display = (
        "__str__",
        "industry_display",
        "company_size",
        "headquarters",
        "followers",
        "job_count",
        "detail_status",
        "website_link",
        "linkedin_link",
    )
    list_filter = ("detail_status", "industry", "company_size", "company_type")
    search_fields = ("name", "slug", "industry", "headquarters")
    list_per_page = 50
    actions = [export_csv, export_xlsx, requeue_details]
    readonly_fields = ("first_seen_at", "detail_fetched_at", "detail_claimed_at")
    fieldsets = (
        (None, {"fields": ("name", "linkedin_url", "slug", "website", "tagline")}),
        (
            "About",
            {
                "fields": (
                    "industry",
                    "company_size",
                    "employees_on_linkedin",
                    "headquarters",
                    "address",
                    "company_type",
                    "founded",
                    "followers",
                    "specialties",
                    "description",
                    "logo_url",
                )
            },
        ),
        (
            "Processing",
            {
                "fields": (
                    "detail_status",
                    "detail_attempts",
                    "detail_error",
                    "detail_claimed_at",
                    "detail_fetched_at",
                    "first_seen_at",
                )
            },
        ),
    )

    def get_queryset(self, request):
        industry_from_jobs = Subquery(
            Job.objects.filter(company=OuterRef("pk")).exclude(industries="").values("industries")[:1]
        )
        return super().get_queryset(request).annotate(_job_count=Count("jobs"), industry_from_jobs=industry_from_jobs)

    @admin.display(description="Industry", ordering="industry")
    def industry_display(self, obj):
        if obj.industry:
            return obj.industry
        if obj.industry_from_jobs:
            # Company page not fetched: the industry LinkedIn shows on the company's postings.
            return format_html('{} <span style="opacity:.6">(from jobs)</span>', obj.industry_from_jobs)
        return "—"

    @admin.display(description="Jobs", ordering="_job_count")
    def job_count(self, obj):
        url = reverse("admin:jobs_job_changelist") + f"?company__id__exact={obj.pk}"
        return format_html('<a href="{}">{}</a>', url, obj._job_count)

    @admin.display(description="Website")
    def website_link(self, obj):
        return _open_link(obj.website, "site")

    @admin.display(description="LinkedIn")
    def linkedin_link(self, obj):
        return _open_link(obj.linkedin_url)

from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

admin.site.site_header = "LinkedIn jobs parser"
admin.site.site_title = "LinkedIn jobs parser"

urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("admin/", admin.site.urls),
]

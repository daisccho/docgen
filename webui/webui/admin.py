from django.contrib import admin

from .models import DocumentVersion, Job, Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "repository_url", "default_branch", "updated_at")
    search_fields = ("name", "slug", "repository_url")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "kind", "status", "created_at", "finished_at")
    list_filter = ("kind", "status")
    readonly_fields = ("created_at", "started_at", "finished_at")


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = ("project", "release_tag", "commit_hash", "documents_count", "created_at")
    search_fields = ("project__name", "release_tag", "commit_hash")

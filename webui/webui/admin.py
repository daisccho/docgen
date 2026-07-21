from django.contrib import admin

from .models import DocumentVersion, GlobalSettings, Job, Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "repository_url", "updated_at")
    search_fields = ("name", "slug", "repository_url")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "kind",
        "status",
        "retry_count",
        "created_at",
        "finished_at",
    )
    list_filter = ("kind", "status")
    readonly_fields = ("created_at", "started_at", "finished_at")


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = ("project", "release_tag", "commit_hash", "documents_count", "created_at")
    search_fields = ("project__name", "release_tag", "commit_hash")


@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "llm_model",
        "llm_base_url",
        "max_iterations",
        "updated_at",
    )

    def has_add_permission(self, request):
        return not GlobalSettings.objects.exists()

from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.system_settings, name="system-settings"),
    path("help/", views.user_guide, name="user-guide"),
    path("projects/new/", views.project_create, name="project-create"),
    path("projects/<slug:slug>/", views.project_detail, name="project-detail"),
    path(
        "projects/<slug:slug>/settings/",
        views.project_update,
        name="project-update",
    ),
    path("projects/<slug:slug>/snapshot/", views.snapshot_create, name="snapshot-create"),
    path("projects/<slug:slug>/reinit/", views.reinit_project, name="reinit-project"),
    path("projects/<slug:slug>/watch/", views.watch_create, name="watch-create"),
    path("projects/<slug:slug>/watch/monitor/", views.watch_monitor, name="watch-monitor"),
    path("projects/<slug:slug>/watch/stop/", views.watch_stop, name="watch-stop"),
    path("projects/<slug:slug>/actions/", views.project_actions_partial, name="project-actions-partial"),
    path(
        "projects/<slug:slug>/versions/<int:version_id>/",
        views.version_detail,
        name="version-detail",
    ),
    path(
        "projects/<slug:slug>/versions/<int:version_id>/documents/<path:document_path>",
        views.document_detail,
        name="document-detail",
    ),
    path("jobs/<int:pk>/", views.job_detail, name="job-detail"),
    path("jobs/<int:pk>/retry/", views.job_retry, name="job-retry"),
]

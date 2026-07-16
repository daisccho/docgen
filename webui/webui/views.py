from __future__ import annotations

from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import ProjectForm
from .models import DocumentVersion, Job, Project
from .services import _check_stale_watch_jobs, enqueue_job, safe_document_path, sync_versions


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    projects = Project.objects.prefetch_related("versions", "jobs").all()
    recent_jobs = Job.objects.select_related("project", "requested_by")[:10]
    return render(
        request,
        "webui/dashboard.html",
        {"projects": projects, "recent_jobs": recent_jobs},
    )


@login_required
def project_create(request: HttpRequest) -> HttpResponse:
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.created_by = request.user
        project.save()
        enqueue_job(project, Job.Kind.INITIALIZE, request.user)
        messages.success(request, "Проект создан; инициализация добавлена в очередь.")
        return redirect(project)
    return render(request, "webui/project_form.html", {"form": form})


@login_required
def project_update(request: HttpRequest, slug: str) -> HttpResponse:
    project = get_object_or_404(Project, slug=slug)
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Настройки проекта сохранены.")
        return redirect(project)
    return render(
        request,
        "webui/project_form.html",
        {"form": form, "project": project, "editing": True},
    )


@login_required
def project_detail(request: HttpRequest, slug: str) -> HttpResponse:
    project = get_object_or_404(Project, slug=slug)
    sync_versions(project)
    _check_stale_watch_jobs(project)
    init_status, init_label = _compute_init_status(project)
    watch_active = project.jobs.filter(
        kind=Job.Kind.WATCH, status__in=[Job.Status.QUEUED, Job.Status.RUNNING]
    ).exists()
    return render(
        request,
        "webui/project_detail.html",
        {
            "project": project,
            "jobs": project.jobs.select_related("requested_by")[:20],
            "versions": project.versions.all()[:20],
            "init_status": init_status,
            "init_label": init_label,
            "watch_active": watch_active,
        },
    )


def _compute_init_status(project: Project) -> tuple[str, str]:
    """Определить статус инициализации проекта."""
    if (project.workspace_path / ".docgen.yaml").is_file():
        return "succeeded", "Готов"
    last_init = project.jobs.filter(kind=Job.Kind.INITIALIZE).first()
    if last_init:
        return last_init.status, last_init.get_status_display()
    return "queued", "Не инициализирован"


@login_required
def project_actions_partial(request: HttpRequest, slug: str) -> HttpResponse:
    """HTMX-partial: блок .actions с актуальным статусом инициализации."""
    project = get_object_or_404(Project, slug=slug)
    init_status, init_label = _compute_init_status(project)
    return render(
        request,
        "webui/_project_actions.html",
        {
            "project": project,
            "init_status": init_status,
            "init_label": init_label,
            "has_failed_init": init_status == "failed",
        },
    )


@login_required
@require_POST
def reinit_project(request: HttpRequest, slug: str) -> HttpResponse:
    """Повторная инициализация проекта."""
    project = get_object_or_404(Project, slug=slug)
    enqueue_job(project, Job.Kind.INITIALIZE, request.user)
    messages.success(request, "Повторная инициализация добавлена в очередь.")
    return redirect(project)


@login_required
@require_POST
def watch_create(request: HttpRequest, slug: str) -> HttpResponse:
    """Запустить режим наблюдения (watch)."""
    project = get_object_or_404(Project, slug=slug)
    if project.jobs.filter(
        kind=Job.Kind.WATCH, status__in=[Job.Status.QUEUED, Job.Status.RUNNING]
    ).exists():
        messages.error(request, "Режим наблюдения уже запущен.")
        return redirect(project)
    if not (project.workspace_path / ".docgen.yaml").is_file():
        messages.error(request, "Сначала дождитесь завершения инициализации проекта.")
        return redirect(project)
    enqueue_job(project, Job.Kind.WATCH, request.user, log=True)
    messages.success(request, "Режим наблюдения запущен.")
    return redirect(project)


@login_required
@require_POST
def snapshot_create(request: HttpRequest, slug: str) -> HttpResponse:
    project = get_object_or_404(Project, slug=slug)
    if not (project.workspace_path / ".docgen.yaml").is_file():
        messages.error(request, "Сначала дождитесь завершения инициализации проекта.")
        return redirect(project)
    job = enqueue_job(
        project,
        Job.Kind.SNAPSHOT,
        request.user,
        release=request.POST.get("release", "").strip(),
        check=request.POST.get("check") == "on",
        log=True,
    )
    messages.success(request, f"Задание #{job.pk} добавлено в очередь.")
    return redirect("job-detail", pk=job.pk)


@login_required
def job_detail(request: HttpRequest, pk: int) -> HttpResponse:
    job = get_object_or_404(Job.objects.select_related("project", "requested_by"), pk=pk)
    # Если есть log_path — читаем лог-файл напрямую
    file_output = None
    if job.log_path:
        try:
            with open(job.log_path, "r", encoding="utf-8", errors="replace") as f:
                file_output = f.read()
        except (FileNotFoundError, IOError):
            pass
    template = "webui/_job_output.html" if request.GET.get("partial") == "1" else "webui/job_detail.html"
    return render(request, template, {"job": job, "file_output": file_output})


@login_required
def version_detail(request: HttpRequest, slug: str, version_id: int) -> HttpResponse:
    project = get_object_or_404(Project, slug=slug)
    version = get_object_or_404(DocumentVersion, project=project, pk=version_id)
    root = project.workspace_path / version.directory_name
    documents = [str(path.relative_to(root)).replace("\\", "/") for path in sorted(root.rglob("*.md"))]
    return render(
        request,
        "webui/version_detail.html",
        {"project": project, "version": version, "documents": documents},
    )


@login_required
def document_detail(
    request: HttpRequest, slug: str, version_id: int, document_path: str
) -> HttpResponse:
    project = get_object_or_404(Project, slug=slug)
    version = get_object_or_404(DocumentVersion, project=project, pk=version_id)
    try:
        path = safe_document_path(project, version.directory_name, document_path)
    except (ValueError, FileNotFoundError):
        raise Http404("Документ не найден")
    return render(
        request,
        "webui/document_detail.html",
        {
            "project": project,
            "version": version,
            "document_path": document_path,
            "content": path.read_text(encoding="utf-8", errors="replace"),
        },
    )


@login_required
def watch_monitor(request: HttpRequest, slug: str) -> HttpResponse:
    """Страница мониторинга активного наблюдения."""
    project = get_object_or_404(Project, slug=slug)
    _check_stale_watch_jobs(project)
    watch_job = (
        project.jobs.filter(
            kind=Job.Kind.WATCH,
            status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
        )
        .select_related("project")
        .first()
    )
    if not watch_job:
        messages.info(request, "Нет активного наблюдения.")
        return redirect(project)
    return render(request, "webui/watch_monitor.html", {"job": watch_job})


@login_required
@require_POST
def watch_stop(request: HttpRequest, slug: str) -> HttpResponse:
    """Остановить активное наблюдение."""
    import os, signal
    from django.utils import timezone

    project = get_object_or_404(Project, slug=slug)
    watch_job = project.jobs.filter(
        kind=Job.Kind.WATCH,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).first()
    if not watch_job:
        messages.info(request, "Нет активного наблюдения.")
        return redirect(project)

    pid = watch_job.parameters.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    watch_job.status = Job.Status.CANCELLED
    watch_job.finished_at = timezone.now()
    watch_job.save(update_fields=["status", "finished_at"])

    messages.success(request, f"Наблюдение #{watch_job.pk} остановлено.")
    return redirect(project)

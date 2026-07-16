from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import DocumentVersion, Job, Project
from core.git_analyzer import sanitize_tag_name


COMMIT_DIR = re.compile(r"^[0-9a-f]{40}$")


def enqueue_job(project: Project, kind: str, user=None, **parameters) -> Job:
    return Job.objects.create(
        project=project,
        kind=kind,
        requested_by=user if getattr(user, "is_authenticated", False) else None,
        parameters=parameters,
    )


def claim_next_job() -> Job | None:
    """Atomically claim the oldest queued job.

    PostgreSQL provides proper row locking. SQLite is sufficient for one MVP
    worker, which is the supported development configuration.
    """
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(status=Job.Status.QUEUED)
            .order_by("created_at")
            .first()
        )
        if not job:
            return None
        job.status = Job.Status.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])
        return job


def _check_stale_watch_jobs(project) -> None:
    """Проверить живость WATCH-процессов, мёртвые перевести в FAILED."""
    import os
    import signal

    for job in project.jobs.filter(
        kind=Job.Kind.WATCH, status=Job.Status.RUNNING
    ):
        pid = job.parameters.get("pid")
        if pid is None:
            continue
        try:
            os.kill(int(pid), signal.SIG_DFL)
        except (OSError, ProcessLookupError):
            job.status = Job.Status.FAILED
            job.error = (job.error or "") + "\nПроцесс наблюдения неожиданно завершился."
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error", "finished_at"])
        except (ValueError, TypeError):
            pass


def execute_job(job: Job) -> None:
    try:
        if job.kind != Job.Kind.INITIALIZE:
            sync_project_runtime_config(job.project)

        # Для WATCH и SNAPSHOT — вычисляем log_path до build_command
        if job.kind in (Job.Kind.WATCH, Job.Kind.SNAPSHOT):
            kind_dir = "watch" if job.kind == Job.Kind.WATCH else "snapshot"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = job.project.workspace_path / "logs" / f"{kind_dir}_{stamp}.log"
            job.log_path = str(log_path)

        command = build_command(job)
        workspace = job.project.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        env = build_job_environment(job.project)

        # Watch — долгоживущий процесс, запускаем в фоне
        if job.kind == Job.Kind.WATCH:
            proc = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            job.parameters = {**job.parameters, "pid": proc.pid}
            job.save(update_fields=["parameters", "log_path"])
            return

        result = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(job.parameters.get("timeout", 3600)),
        )
        job.output = (result.stdout or "")[-200_000:]
        if result.returncode:
            job.status = Job.Status.FAILED
            job.error = (result.stderr or f"Process exited with {result.returncode}")[-50_000:]
        else:
            job.status = Job.Status.SUCCEEDED
            sync_versions(job.project, generated_by=job)
    except Exception as exc:
        job.status = Job.Status.FAILED
        job.error = str(exc)
    finally:
        if job.kind != Job.Kind.WATCH:
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "output", "error", "finished_at"])


def build_command(job: Job) -> list[str]:
    python = settings.DOCGEN_PYTHON or sys.executable
    # Ищем docgen CLI (установленный entry point) в том же окружении
    import shutil
    docgen_exe = shutil.which("docgen")
    if docgen_exe:
        base = [docgen_exe]
    else:
        # Fallback: в том же каталоге, что и python
        bin_dir = os.path.dirname(python)
        docgen_exe = os.path.join(bin_dir, "docgen")
        base = [docgen_exe] if os.path.exists(docgen_exe) else [python, "-m", "docgen"]
    project = job.project

    if job.kind == Job.Kind.INITIALIZE:
        command = base + [
            "init",
            "--repo",
            project.repository_url,
            "--model",
            project.llm_model,
            "--project",
            project.slug,
            "-i",
            str(project.max_iterations),
            "--provider",
            project.llm_provider,
        ]
        if project.llm_base_url:
            command += ["--base-url", project.llm_base_url]
        github_token_env = (
            "DOCGEN_GITHUB_TOKEN" if project.has_github_token else project.github_token_env
        )
        if github_token_env:
            command += ["--github-token-env", github_token_env]
        return command

    if job.kind == Job.Kind.SNAPSHOT:
        command = base + ["snapshot", "-v"]
        if job.log_path:
            command += ["--log-file", job.log_path]
        else:
            command += ["--log"]
        if job.parameters.get("release"):
            command += ["--release", str(job.parameters["release"])]
        if job.parameters.get("check"):
            command.append("--check")
        return command

    if job.kind == Job.Kind.WATCH:
        command = base + ["watch", "-v", "-t", str(job.project.watch_interval)]
        if job.log_path:
            command += ["--log-file", job.log_path]
        else:
            command += ["-l"]
        return command

    raise ValueError(f"Unsupported job kind: {job.kind}")


def build_job_environment(project: Project) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    if project.has_llm_api_key:
        env["OPENAI_API_KEY"] = project.get_llm_api_key()
    elif project.api_key_env and project.api_key_env != "OPENAI_API_KEY":
        api_key = env.get(project.api_key_env)
        if api_key:
            env["OPENAI_API_KEY"] = api_key

    if project.has_github_token:
        env["DOCGEN_GITHUB_TOKEN"] = project.get_github_token()
    if project.llm_base_url:
        env["OPENAI_BASE_URL"] = project.llm_base_url
    return env


def sync_project_runtime_config(project: Project) -> None:
    """Keep non-secret CLI settings aligned after edits in the WebUI."""
    config_path = project.workspace_path / ".docgen.yaml"
    if not config_path.is_file():
        return
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, TypeError):
        return
    config = data.setdefault("config", {})
    config["llm_model"] = project.llm_model
    config["llm_provider"] = project.llm_provider
    config["llm_base_url"] = project.llm_base_url or None
    config["llm_api_key"] = None
    config["github_token_env"] = (
        "DOCGEN_GITHUB_TOKEN" if project.has_github_token else project.github_token_env or None
    )
    config_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def sync_versions(project: Project, generated_by: Job | None = None) -> None:
    root = project.workspace_path
    if not root.exists():
        return

    release_map_path = root / ".release-map.yaml"
    releases: dict[str, str] = {}
    if release_map_path.is_file():
        try:
            data = yaml.safe_load(release_map_path.read_text(encoding="utf-8")) or {}
            releases = data.get("releases") or {}
        except (OSError, yaml.YAMLError, TypeError):
            releases = {}

    known_directories: set[str] = set()
    for release_tag, commit_hash in releases.items():
        directory_name = sanitize_tag_name(str(release_tag))
        entry = root / directory_name
        if not entry.is_dir():
            continue
        known_directories.add(directory_name)
        count = sum(1 for _ in entry.rglob("*.md"))
        DocumentVersion.objects.update_or_create(
            project=project,
            directory_name=directory_name,
            defaults={
                "release_tag": str(release_tag),
                "commit_hash": str(commit_hash),
                "documents_count": count,
                "generated_by": generated_by,
            },
        )

    # HEAD fallback remains a SHA-named directory when a repository has no tags.
    for entry in root.iterdir():
        if (
            not entry.is_dir()
            or entry.name in known_directories
            or not COMMIT_DIR.fullmatch(entry.name)
        ):
            continue
        count = sum(1 for _ in entry.rglob("*.md"))
        DocumentVersion.objects.update_or_create(
            project=project,
            directory_name=entry.name,
            defaults={
                "release_tag": "",
                "commit_hash": entry.name,
                "documents_count": count,
                "generated_by": generated_by,
            },
        )


def safe_document_path(project: Project, directory_name: str, relative_path: str) -> Path:
    version_root = (project.workspace_path / directory_name).resolve()
    candidate = (version_root / relative_path).resolve()
    if version_root != candidate and version_root not in candidate.parents:
        raise ValueError("Недопустимый путь документа")
    if candidate.suffix.lower() != ".md" or not candidate.is_file():
        raise FileNotFoundError(relative_path)
    return candidate

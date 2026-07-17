import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.agent import DocAgent
from core.config import init_project
from core.errors import DocAgentError, RefNotFoundError
from core.git_analyzer import get_snapshot_versions
from .models import GlobalSettings, Job, Project
from .services import (
    build_command,
    build_job_environment,
    retry_job,
    safe_document_path,
    sync_versions,
)


class WebuiTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.user = users.objects.create_user("writer", password="secret")
        self.staff = users.objects.create_user(
            "admin", password="secret", is_staff=True
        )
        self.client.force_login(self.user)
        self.project = Project.objects.create(
            name="Example",
            slug="example",
            repository_url="https://example.org/team/repo.git",
            created_by=self.user,
        )

    def test_dashboard_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_dashboard_lists_project_name_and_identifier(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Example")
        self.assertContains(response, "example")

    def test_project_form_contains_only_project_runtime_fields(self):
        response = self.client.get(reverse("project-create"))
        self.assertEqual(response.context["form"]["watch_interval"].value(), 60)
        for field in (
            "name",
            "repository_url",
            "watch_interval",
        ):
            self.assertContains(response, f'name="{field}"')
        for field in (
            "slug",
            "default_branch",
            "llm_model",
            "llm_base_url",
            "max_iterations",
            "llm_api_key",
            "github_token",
            "llm_provider",
            "max_iterations",
        ):
            self.assertNotContains(response, f'name="{field}"')

    def test_project_watch_interval_defaults_to_sixty_minutes(self):
        self.assertEqual(self.project.watch_interval, 60)

    def test_project_creation_generates_identifier_and_enqueues_init(self):
        response = self.client.post(
            reverse("project-create"),
            {
                "name": "Other",
                "repository_url": "https://example.org/other.git",
                "watch_interval": 15,
            },
        )
        self.assertRedirects(response, reverse("project-detail", args=["other"]))
        project = Project.objects.get(slug="other")
        self.assertEqual(project.watch_interval, 15)
        self.assertTrue(
            Job.objects.filter(
                project=project, kind=Job.Kind.INITIALIZE
            ).exists()
        )

    def test_project_identifier_is_generated_and_made_unique(self):
        first = Project.objects.create(
            name="Повторяющееся название",
            repository_url="https://example.org/team/docs.git",
        )
        second = Project.objects.create(
            name="Повторяющееся название",
            repository_url="https://example.org/team/docs.git",
        )
        self.assertEqual(first.slug, "docs")
        self.assertEqual(second.slug, "docs-2")

    def test_regular_user_cannot_open_global_settings(self):
        response = self.client.get(reverse("system-settings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_staff_can_store_and_clear_global_encrypted_secrets(self):
        self.client.force_login(self.staff)
        settings_page = self.client.get(reverse("system-settings"))
        for field in (
            "llm_model",
            "llm_base_url",
            "llm_api_key",
            "github_token",
        ):
            self.assertContains(settings_page, f'name="{field}"')
        self.assertNotContains(settings_page, 'name="watch_interval"')
        self.assertNotContains(settings_page, 'name="llm_provider"')

        response = self.client.post(
            reverse("system-settings"),
            {
                "llm_model": "deepseek-v4-flash-free",
                "llm_base_url": "https://opencode.ai/zen/v1",
                "max_iterations": 17,
                "llm_api_key": "test-llm-secret",
                "github_token": "test-github-secret",
            },
        )
        self.assertRedirects(response, reverse("system-settings"))
        common = GlobalSettings.load()
        self.assertEqual(common.llm_model, "deepseek-v4-flash-free")
        self.assertEqual(common.max_iterations, 17)
        self.assertNotIn("test-llm-secret", common.llm_api_key_encrypted)
        self.assertEqual(common.get_llm_api_key(), "test-llm-secret")
        self.assertEqual(common.get_github_token(), "test-github-secret")

        response = self.client.post(
            reverse("system-settings"),
            {
                "llm_model": common.llm_model,
                "llm_base_url": common.llm_base_url,
                "max_iterations": common.max_iterations,
                "clear_llm_api_key": "on",
            },
        )
        self.assertRedirects(response, reverse("system-settings"))
        common.refresh_from_db()
        self.assertFalse(common.has_llm_api_key)

    def test_init_command_uses_global_model_without_secrets_or_provider(self):
        common = GlobalSettings.load()
        common.llm_model = "deepseek-v4-flash-free"
        common.llm_base_url = "https://opencode.ai/zen/v1"
        common.max_iterations = 23
        common.set_github_token("github-secret")
        common.save()
        job = Job.objects.create(project=self.project, kind=Job.Kind.INITIALIZE)
        command = build_command(job)
        self.assertEqual(command[1:3], ["-m", "core"])
        self.assertIn("--repo", command)
        self.assertNotIn("--branch", command)
        self.assertIn("deepseek-v4-flash-free", command)
        self.assertIn("23", command)
        self.assertIn("https://opencode.ai/zen/v1", command)
        self.assertIn("DOCGEN_GITHUB_TOKEN", command)
        self.assertNotIn("--api-key", command)
        self.assertNotIn("github-secret", command)
        self.assertNotIn("--provider", command)

    def test_global_secrets_are_injected_into_worker_environment(self):
        common = GlobalSettings.load()
        common.set_llm_api_key("llm-secret")
        common.set_github_token("github-secret")
        common.save()
        env = build_job_environment(self.project)
        self.assertEqual(env["OPENAI_API_KEY"], "llm-secret")
        self.assertEqual(env["DOCGEN_GITHUB_TOKEN"], "github-secret")

    def test_global_secrets_override_legacy_project_secrets(self):
        self.project.set_llm_api_key("old-project-key")
        self.project.set_github_token("old-project-token")
        self.project.save()
        common = GlobalSettings.load()
        common.set_llm_api_key("global-key")
        common.set_github_token("global-token")
        common.save()
        env = build_job_environment(self.project)
        self.assertEqual(env["OPENAI_API_KEY"], "global-key")
        self.assertEqual(env["DOCGEN_GITHUB_TOKEN"], "global-token")

    def test_failed_job_can_be_retried_without_new_id(self):
        job = Job.objects.create(
            project=self.project,
            kind=Job.Kind.WATCH,
            status=Job.Status.FAILED,
            parameters={"pid": 12345, "log": True},
            log_path="old.log",
            error="401 Unauthorized",
        )
        self.assertIn("API-ключ", job.error_hint)
        original_id = job.pk
        response = self.client.post(reverse("job-retry", args=[job.pk]))
        self.assertRedirects(response, reverse("job-detail", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.pk, original_id)
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.retry_count, 1)
        self.assertNotIn("pid", job.parameters)
        self.assertEqual(job.log_path, "")
        self.assertEqual(Job.objects.filter(pk=original_id).count(), 1)

    def test_retry_rejects_active_job(self):
        job = Job.objects.create(
            project=self.project,
            kind=Job.Kind.SNAPSHOT,
            status=Job.Status.RUNNING,
        )
        self.assertFalse(retry_job(job))

    def test_failed_job_page_shows_hint_retry_and_log_highlighting(self):
        job = Job.objects.create(
            project=self.project,
            kind=Job.Kind.SNAPSHOT,
            status=Job.Status.FAILED,
            error="401 Unauthorized",
        )
        response = self.client.get(reverse("job-detail", args=[job.pk]))
        self.assertContains(response, "Как исправить")
        self.assertContains(response, "Повторить выполнение")
        self.assertContains(response, "ln.includes(token)")

    def test_user_guide_describes_watch_without_powershell_section(self):
        response = self.client.get(reverse("user-guide"))
        self.assertContains(response, "Режим наблюдения")
        self.assertNotContains(response, "Запуск в Windows PowerShell")

    def test_snapshot_command_uses_release_and_log_file(self):
        job = Job.objects.create(
            project=self.project,
            kind=Job.Kind.SNAPSHOT,
            parameters={"release": "v2.0.0"},
            log_path="snapshot.log",
        )
        command = build_command(job)
        self.assertIn("--release", command)
        self.assertIn("v2.0.0", command)
        self.assertIn("--log-file", command)
        self.assertIn("snapshot.log", command)

    def test_watch_command_uses_project_interval(self):
        self.project.watch_interval = 27
        self.project.save(update_fields=["watch_interval"])
        job = Job.objects.create(project=self.project, kind=Job.Kind.WATCH)
        command = build_command(job)
        self.assertIn("watch", command)
        self.assertIn("27", command)
        self.assertNotIn("--branch", command)

    def test_snapshot_stops_when_repository_has_no_release_tags(self):
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            try:
                os.chdir(root)
                state = init_project("https://example.org/repo.git")
                agent = DocAgent(state)
                with (
                    patch("core.agent.ensure_clone"),
                    patch("core.agent.fetch_tags"),
                    patch("core.agent.get_latest_tag", return_value=None),
                ):
                    with self.assertRaisesRegex(
                        DocAgentError, "отсутствуют релизные теги"
                    ):
                        agent.snapshot()
            finally:
                os.chdir(previous)

    def test_snapshot_rejects_branch_instead_of_release_tag(self):
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            try:
                os.chdir(root)
                state = init_project("https://example.org/repo.git")
                agent = DocAgent(state)
                with (
                    patch("core.agent.ensure_clone"),
                    patch("core.agent.fetch_tags"),
                    patch(
                        "core.agent.get_all_tags_with_hash",
                        return_value={"v1.0.0": "a" * 40},
                    ),
                ):
                    with self.assertRaisesRegex(
                        RefNotFoundError, "Релизный тег не найден"
                    ):
                        agent.snapshot(release_tag="main")
            finally:
                os.chdir(previous)

    def test_document_path_cannot_escape_snapshot(self):
        with TemporaryDirectory() as root:
            with override_settings(DOCGEN_WORKSPACE_ROOT=Path(root)):
                version = self.project.workspace_path / ("a" * 40)
                version.mkdir(parents=True)
                (version / "README.md").write_text("ok", encoding="utf-8")
                self.assertEqual(
                    safe_document_path(
                        self.project, "a" * 40, "README.md"
                    ).name,
                    "README.md",
                )
                with self.assertRaises((ValueError, FileNotFoundError)):
                    safe_document_path(
                        self.project, "a" * 40, "../secret.md"
                    )

    def test_release_map_is_synchronized(self):
        with TemporaryDirectory() as root:
            with override_settings(DOCGEN_WORKSPACE_ROOT=Path(root)):
                workspace = self.project.workspace_path
                version_dir = workspace / "release-v2"
                version_dir.mkdir(parents=True)
                (version_dir / "README.md").write_text(
                    "docs", encoding="utf-8"
                )
                (workspace / ".release-map.yaml").write_text(
                    "last_documented_release: release/v2\n"
                    "releases:\n"
                    f"  release/v2: {'b' * 40}\n",
                    encoding="utf-8",
                )
                sync_versions(self.project)
                version = self.project.versions.get()
                self.assertEqual(version.release_tag, "release/v2")
                self.assertEqual(version.directory_name, "release-v2")
                self.assertEqual(version.documents_count, 1)

    def test_core_version_scan_excludes_service_directories(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / "logs").mkdir()
            (root_path / "v1.0.0").mkdir()
            versions = get_snapshot_versions(root)
            self.assertEqual(
                [item["name"] for item in versions], ["v1.0.0"]
            )

    def test_agentic_mapping_uses_llm_json_result(self):
        agent = DocAgent.__new__(DocAgent)
        agent.verbose = False
        agent._clone_dir = ".clone"
        agent._generator = SimpleNamespace(_client=object())
        agent._run_tool_loop = lambda *args, **kwargs: (
            '["README.md", "docs/guide.md"]'
        )
        selected = agent._map_changes_to_docs_agentic(
            [],
            ["README.md", "docs/guide.md"],
            {"README.md", "docs/guide.md"},
            "a" * 40,
            "b" * 40,
            3,
        )
        self.assertEqual(selected, {"README.md", "docs/guide.md"})

    def test_snapshot_scan_returns_portable_paths(self):
        with TemporaryDirectory() as root:
            nested = Path(root) / "docs"
            nested.mkdir()
            (nested / "guide.md").write_text("docs", encoding="utf-8")
            self.assertEqual(
                DocAgent._scan_snapshot_md(root), ["docs/guide.md"]
            )

    def test_agent_terminal_blocks_write_commands(self):
        self.assertIsNotNone(
            DocAgent._terminal_write_violation(
                "python -c \"open('README.md', 'w').write('bad')\""
            )
        )
        self.assertIsNone(
            DocAgent._terminal_write_violation(
                "git show HEAD:README.md 2>/dev/null"
            )
        )

    def test_audit_response_requires_document_markers(self):
        agent = DocAgent.__new__(DocAgent)
        agent.verbose = False
        self.assertIsNone(
            agent._extract_document_response(
                "Документация актуальна.\n\n# README"
            )
        )
        self.assertEqual(
            agent._extract_document_response(
                "DOCGEN_DOCUMENT_START\n# README\n\nТекст.\n"
                "DOCGEN_DOCUMENT_END"
            ),
            "# README\n\nТекст.",
        )

    def test_environment_api_key_is_not_persisted_to_yaml(self):
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            try:
                os.chdir(root)
                with patch.dict(
                    os.environ, {"OPENAI_API_KEY": "environment-secret"}
                ):
                    state = init_project("https://example.org/repo.git")
                self.assertIsNone(state.config.llm_api_key)
                contents = Path(".docgen.yaml").read_text(encoding="utf-8")
                self.assertNotIn("environment-secret", contents)
            finally:
                os.chdir(previous)

from pathlib import Path
from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.git_analyzer import get_snapshot_versions
from .models import Job, Project
from .services import (
    build_command,
    build_job_environment,
    safe_document_path,
    sync_versions,
)


class WebuiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("writer", password="secret")
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

    def test_dashboard_lists_project(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Example")

    def test_project_creation_enqueues_init(self):
        response = self.client.post(
            reverse("project-create"),
            {
                "name": "Other",
                "slug": "other",
                "repository_url": "https://example.org/other.git",
                "default_branch": "main",
                "llm_model": "gpt-4o",
                "llm_base_url": "",
                "llm_api_key": "test-llm-secret",
                "github_token": "test-github-secret",
            },
        )
        self.assertRedirects(response, reverse("project-detail", args=["other"]))
        self.assertTrue(Job.objects.filter(project__slug="other", kind=Job.Kind.INITIALIZE).exists())
        project = Project.objects.get(slug="other")
        self.assertNotIn("test-llm-secret", project.llm_api_key_encrypted)
        self.assertNotIn("test-github-secret", project.github_token_encrypted)
        self.assertEqual(project.get_llm_api_key(), "test-llm-secret")
        self.assertEqual(project.get_github_token(), "test-github-secret")

    def test_init_command_does_not_put_secret_in_arguments(self):
        job = Job.objects.create(project=self.project, kind=Job.Kind.INITIALIZE)
        command = build_command(job)
        self.assertIn("--repo", command)
        self.assertNotIn("--api-key", command)

    def test_init_command_uses_github_token_environment_name(self):
        self.project.github_token_env = "GITHUB_TOKEN"
        self.project.save(update_fields=["github_token_env"])
        job = Job.objects.create(project=self.project, kind=Job.Kind.INITIALIZE)
        command = build_command(job)
        self.assertIn("--github-token-env", command)
        self.assertIn("GITHUB_TOKEN", command)

    def test_saved_secrets_are_injected_into_worker_environment(self):
        self.project.set_llm_api_key("llm-secret")
        self.project.set_github_token("github-secret")
        self.project.save()
        env = build_job_environment(self.project)
        self.assertEqual(env["OPENAI_API_KEY"], "llm-secret")
        self.assertEqual(env["DOCGEN_GITHUB_TOKEN"], "github-secret")

        job = Job.objects.create(project=self.project, kind=Job.Kind.INITIALIZE)
        command = build_command(job)
        self.assertIn("DOCGEN_GITHUB_TOKEN", command)
        self.assertNotIn("github-secret", command)

    def test_project_settings_keep_or_clear_existing_secret(self):
        self.project.set_llm_api_key("existing-secret")
        self.project.save()
        base_data = {
            "name": self.project.name,
            "slug": self.project.slug,
            "repository_url": self.project.repository_url,
            "default_branch": self.project.default_branch,
            "llm_model": self.project.llm_model,
            "llm_base_url": "",
        }
        response = self.client.post(
            reverse("project-update", args=[self.project.slug]), base_data
        )
        self.assertRedirects(response, self.project.get_absolute_url())
        self.project.refresh_from_db()
        self.assertEqual(self.project.get_llm_api_key(), "existing-secret")

        response = self.client.post(
            reverse("project-update", args=[self.project.slug]),
            {**base_data, "clear_llm_api_key": "on"},
        )
        self.assertRedirects(response, self.project.get_absolute_url())
        self.project.refresh_from_db()
        self.assertFalse(self.project.has_llm_api_key)

    def test_snapshot_command_uses_release_option(self):
        job = Job.objects.create(
            project=self.project,
            kind=Job.Kind.SNAPSHOT,
            parameters={"release": "v2.0.0"},
        )
        command = build_command(job)
        self.assertIn("--release", command)
        self.assertIn("v2.0.0", command)
        self.assertNotIn("--ref", command)

    def test_document_path_cannot_escape_snapshot(self):
        with TemporaryDirectory() as root:
            with override_settings(DOCGEN_WORKSPACE_ROOT=Path(root)):
                version = self.project.workspace_path / ("a" * 40)
                version.mkdir(parents=True)
                (version / "README.md").write_text("ok", encoding="utf-8")
                self.assertEqual(
                    safe_document_path(self.project, "a" * 40, "README.md").name,
                    "README.md",
                )
                with self.assertRaises((ValueError, FileNotFoundError)):
                    safe_document_path(self.project, "a" * 40, "../secret.md")

    def test_release_map_is_synchronized(self):
        with TemporaryDirectory() as root:
            with override_settings(DOCGEN_WORKSPACE_ROOT=Path(root)):
                workspace = self.project.workspace_path
                version_dir = workspace / "release-v2"
                version_dir.mkdir(parents=True)
                (version_dir / "README.md").write_text("docs", encoding="utf-8")
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
            self.assertEqual([item["name"] for item in versions], ["v1.0.0"])

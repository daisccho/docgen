from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from urllib.parse import urlparse


slug_validator = RegexValidator(
    regex=r"^[a-z0-9][a-z0-9_-]*$",
    message="Используйте строчные латинские буквы, цифры, '-' и '_'.",
)


class Project(models.Model):
    name = models.CharField("Название", max_length=120)
    slug = models.SlugField(
        "Идентификатор", unique=True, blank=True, validators=[slug_validator]
    )
    repository_url = models.CharField("Git-репозиторий", max_length=500)
    default_branch = models.CharField("Основная ветка", max_length=120, default="main")
    llm_model = models.CharField("LLM-модель", max_length=120, default="gpt-4o")
    llm_base_url = models.URLField("Base URL LLM", max_length=500, blank=True)
    api_key_env = models.CharField(
        "Переменная с API-ключом", max_length=120, default="OPENAI_API_KEY"
    )
    github_token_env = models.CharField(
        "Переменная с GitHub-токеном", max_length=120, blank=True
    )
    llm_api_key_encrypted = models.TextField(blank=True, editable=False)
    github_token_encrypted = models.TextField(blank=True, editable=False)
    max_iterations = models.IntegerField(
        "Максимум ходов агента", default=10, blank=True
    )
    watch_interval = models.IntegerField(
        "Интервал наблюдения (мин)", default=60, blank=True
    )
    llm_provider = models.CharField(
        "Провайдер LLM", max_length=60, default="openai", blank=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Создал",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="docgen_projects",
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Изменён", auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Проект"
        verbose_name_plural = "Проекты"

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def _generate_unique_slug(self) -> str:
        base = slugify(self.name)
        if not base:
            repo_path = urlparse(self.repository_url).path.rstrip("/")
            repo_name = repo_path.rsplit("/", 1)[-1].removesuffix(".git")
            base = slugify(repo_name)
        base = (base or "project")[:50]
        candidate = base
        suffix = 2
        while Project.objects.exclude(pk=self.pk).filter(slug=candidate).exists():
            tail = f"-{suffix}"
            candidate = f"{base[:50-len(tail)]}{tail}"
            suffix += 1
        return candidate

    def get_absolute_url(self) -> str:
        return reverse("project-detail", kwargs={"slug": self.slug})

    @property
    def workspace_path(self):
        return settings.DOCGEN_WORKSPACE_ROOT / self.slug

    @property
    def has_llm_api_key(self) -> bool:
        return bool(self.llm_api_key_encrypted)

    @property
    def has_github_token(self) -> bool:
        return bool(self.github_token_encrypted)

    def set_llm_api_key(self, value: str) -> None:
        from .crypto import encrypt_secret

        self.llm_api_key_encrypted = encrypt_secret(value)

    def get_llm_api_key(self) -> str:
        from .crypto import decrypt_secret

        return decrypt_secret(self.llm_api_key_encrypted)

    def set_github_token(self, value: str) -> None:
        from .crypto import encrypt_secret

        self.github_token_encrypted = encrypt_secret(value)

    def get_github_token(self) -> str:
        from .crypto import decrypt_secret

        return decrypt_secret(self.github_token_encrypted)


class GlobalSettings(models.Model):
    """Singleton LLM and GitHub settings inherited by every WebUI project."""

    llm_model = models.CharField(
        "Модель LLM", max_length=120, default="gpt-4o"
    )
    llm_base_url = models.URLField("Base URL LLM", max_length=500, blank=True)
    max_iterations = models.PositiveIntegerField(
        "Максимум ходов агента",
        default=10,
        validators=[MinValueValidator(5), MaxValueValidator(500)],
    )
    llm_api_key_encrypted = models.TextField(blank=True, editable=False)
    github_token_encrypted = models.TextField(blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Общие настройки"
        verbose_name_plural = "Общие настройки"

    def __str__(self) -> str:
        return "Общие настройки docgen"

    @classmethod
    def load(cls) -> "GlobalSettings":
        settings_obj, _ = cls.objects.get_or_create(pk=1)
        return settings_obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @property
    def has_llm_api_key(self) -> bool:
        return bool(self.llm_api_key_encrypted)

    @property
    def has_github_token(self) -> bool:
        return bool(self.github_token_encrypted)

    def set_llm_api_key(self, value: str) -> None:
        from .crypto import encrypt_secret

        self.llm_api_key_encrypted = encrypt_secret(value)

    def get_llm_api_key(self) -> str:
        from .crypto import decrypt_secret

        return decrypt_secret(self.llm_api_key_encrypted)

    def set_github_token(self, value: str) -> None:
        from .crypto import encrypt_secret

        self.github_token_encrypted = encrypt_secret(value)

    def get_github_token(self) -> str:
        from .crypto import decrypt_secret

        return decrypt_secret(self.github_token_encrypted)


class Job(models.Model):
    class Kind(models.TextChoices):
        INITIALIZE = "init", "Инициализация"
        SNAPSHOT = "snapshot", "Snapshot"
        WATCH = "watch", "Наблюдение"

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Выполняется"
        SUCCEEDED = "succeeded", "Завершено"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменено"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField("Тип", max_length=20, choices=Kind.choices)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    parameters = models.JSONField("Параметры", default=dict, blank=True)
    output = models.TextField("Вывод", blank=True)
    log_path = models.TextField("Путь к файлу лога", blank=True)
    error = models.TextField("Ошибка", blank=True)
    retry_count = models.PositiveIntegerField("Повторных запусков", default=0)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="docgen_jobs",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    started_at = models.DateTimeField("Запущено", null=True, blank=True)
    finished_at = models.DateTimeField("Завершено", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Задание"
        verbose_name_plural = "Задания"
        indexes = [
            models.Index(
                fields=["status", "created_at"], name="webui_job_status_4a97a1_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}: {self.get_kind_display()} #{self.pk}"

    @property
    def error_hint(self) -> str:
        text = f"{self.error}\n{self.output}".lower()
        hints = [
            (
                ("401", "unauthorized", "invalid api key"),
                "Проверьте API-ключ в общих настройках и доступ ключа к выбранной модели.",
            ),
            (
                ("403", "forbidden", "permission denied"),
                "Проверьте права GitHub-токена и доступ аккаунта к репозиторию.",
            ),
            (
                ("repository not found", "не найден", "ref '"),
                "Проверьте URL репозитория, тег релиза и права GitHub-токена.",
            ),
            (
                ("name resolution", "dns", "connection", "timeout"),
                "Проверьте сеть сервера, Base URL провайдера и доступность GitHub/LLM API.",
            ),
            (
                ("model", "not found"),
                "Проверьте идентификатор модели и поддерживает ли её выбранный Base URL.",
            ),
            (
                ("api-ключ не указан", "llm не настроен"),
                "Задайте API-ключ LLM на вкладке «Настройки».",
            ),
        ]
        for markers, hint in hints:
            if any(marker in text for marker in markers):
                return hint
        return (
            "Изучите вывод задания, исправьте настройки проекта или общие "
            "настройки, затем повторите выполнение."
        )


class DocumentVersion(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="versions")
    release_tag = models.CharField("Релиз", max_length=255, blank=True)
    directory_name = models.CharField("Каталог", max_length=255)
    commit_hash = models.CharField("Коммит", max_length=40)
    documents_count = models.PositiveIntegerField("Документов", default=0)
    generated_by = models.ForeignKey(
        Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="versions"
    )
    created_at = models.DateTimeField("Обнаружено", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "directory_name"], name="unique_project_version_dir"
            )
        ]
        verbose_name = "Версия документации"
        verbose_name_plural = "Версии документации"

    def __str__(self) -> str:
        return f"{self.project}: {self.release_tag or self.commit_hash[:8]}"

    @property
    def label(self) -> str:
        return self.release_tag or self.commit_hash[:8]

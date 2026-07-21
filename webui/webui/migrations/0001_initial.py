import django.db.models.deletion
import django.core.validators
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="Project",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="Название")),
                ("slug", models.SlugField(unique=True, validators=[django.core.validators.RegexValidator(message="Используйте строчные латинские буквы, цифры, '-' и '_'.", regex="^[a-z0-9][a-z0-9_-]*$")], verbose_name="Идентификатор")),
                ("repository_url", models.CharField(max_length=500, verbose_name="Git-репозиторий")),
                ("default_branch", models.CharField(default="main", max_length=120, verbose_name="Основная ветка")),
                ("llm_model", models.CharField(default="gpt-4o", max_length=120, verbose_name="LLM-модель")),
                ("api_key_env", models.CharField(default="OPENAI_API_KEY", max_length=120, verbose_name="Переменная с API-ключом")),
                ("access_token_env", models.CharField(blank=True, max_length=120, verbose_name="Переменная с Git-токеном")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создан")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Изменён")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="docgen_projects", to=settings.AUTH_USER_MODEL, verbose_name="Создал")),
            ],
            options={"verbose_name": "Проект", "verbose_name_plural": "Проекты", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("init", "Инициализация"), ("snapshot", "Snapshot")], max_length=20, verbose_name="Тип")),
                ("status", models.CharField(choices=[("queued", "В очереди"), ("running", "Выполняется"), ("succeeded", "Завершено"), ("failed", "Ошибка"), ("cancelled", "Отменено")], default="queued", max_length=20, verbose_name="Статус")),
                ("parameters", models.JSONField(blank=True, default=dict, verbose_name="Параметры")),
                ("output", models.TextField(blank=True, verbose_name="Вывод")),
                ("error", models.TextField(blank=True, verbose_name="Ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="Запущено")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="Завершено")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="jobs", to="webui.project")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="docgen_jobs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "Задание", "verbose_name_plural": "Задания", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="DocumentVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("commit_hash", models.CharField(max_length=40, verbose_name="Коммит")),
                ("documents_count", models.PositiveIntegerField(default=0, verbose_name="Документов")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Обнаружено")),
                ("generated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="versions", to="webui.job")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="webui.project")),
            ],
            options={"verbose_name": "Версия документации", "verbose_name_plural": "Версии документации", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(model_name="job", index=models.Index(fields=["status", "created_at"], name="webui_job_status_4a97a1_idx")),
        migrations.AddConstraint(model_name="documentversion", constraint=models.UniqueConstraint(fields=("project", "commit_hash"), name="unique_project_commit")),
    ]

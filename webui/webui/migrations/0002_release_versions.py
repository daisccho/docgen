from django.db import migrations, models


def populate_version_directories(apps, schema_editor):
    DocumentVersion = apps.get_model("webui", "DocumentVersion")
    for version in DocumentVersion.objects.all().iterator():
        version.directory_name = version.commit_hash
        version.save(update_fields=["directory_name"])


class Migration(migrations.Migration):
    dependencies = [("webui", "0001_initial")]

    operations = [
        migrations.RenameField(
            model_name="project",
            old_name="access_token_env",
            new_name="github_token_env",
        ),
        migrations.AlterField(
            model_name="project",
            name="github_token_env",
            field=models.CharField(
                blank=True,
                max_length=120,
                verbose_name="Переменная с GitHub-токеном",
            ),
        ),
        migrations.AddField(
            model_name="documentversion",
            name="release_tag",
            field=models.CharField(blank=True, max_length=255, verbose_name="Релиз"),
        ),
        migrations.AddField(
            model_name="documentversion",
            name="directory_name",
            field=models.CharField(default="", max_length=255, verbose_name="Каталог"),
            preserve_default=False,
        ),
        migrations.RunPython(populate_version_directories, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="documentversion",
            name="unique_project_commit",
        ),
        migrations.AddConstraint(
            model_name="documentversion",
            constraint=models.UniqueConstraint(
                fields=("project", "directory_name"),
                name="unique_project_version_dir",
            ),
        ),
    ]

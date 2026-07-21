from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webui", "0002_release_versions")]

    operations = [
        migrations.AddField(
            model_name="project",
            name="llm_base_url",
            field=models.URLField(blank=True, max_length=500, verbose_name="Base URL LLM"),
        ),
        migrations.AddField(
            model_name="project",
            name="llm_api_key_encrypted",
            field=models.TextField(blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="project",
            name="github_token_encrypted",
            field=models.TextField(blank=True, editable=False),
        ),
    ]

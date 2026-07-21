from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("webui", "0011_alter_project_watch_interval"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="project",
            name="default_branch",
        ),
    ]

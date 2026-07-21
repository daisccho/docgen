from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webui", "0010_globalsettings_max_iterations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="watch_interval",
            field=models.IntegerField(
                blank=True,
                default=60,
                verbose_name="Интервал наблюдения (мин)",
            ),
        ),
    ]

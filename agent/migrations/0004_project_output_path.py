# Generated manually for project-scoped artifact output folders.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agent", "0003_agentrun_agenttaskrecord"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="output_path",
            field=models.CharField(blank=True, max_length=700),
        ),
    ]

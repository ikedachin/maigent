from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("agent", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectAccessPath",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.CharField(max_length=700)),
                ("mode", models.CharField(choices=[("read", "Read only"), ("write", "Read and write")], default="read", max_length=20)),
                ("note", models.CharField(blank=True, max_length=180)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="access_paths", to="agent.project")),
            ],
            options={
                "ordering": ["mode", "path"],
                "constraints": [
                    models.UniqueConstraint(fields=("project", "path", "mode"), name="unique_project_access_path_mode"),
                ],
            },
        ),
    ]

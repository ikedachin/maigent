# Generated manually for multi-agent worker execution tracking.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agent", "0004_project_output_path"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentWorkerRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80)),
                ("role", models.CharField(max_length=40)),
                ("purpose", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("complete", "Complete"),
                            ("error", "Error"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("result", models.TextField(blank=True)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="worker_runs",
                        to="agent.agentrun",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddField(
            model_name="agenttaskrecord",
            name="worker",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="task_records",
                to="agent.agentworkerrun",
            ),
        ),
    ]

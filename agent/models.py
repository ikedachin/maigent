from django.db import models


class Project(models.Model):
    name = models.CharField(max_length=120)
    path = models.CharField(max_length=500, blank=True)
    output_path = models.CharField(max_length=700, blank=True)
    description = models.TextField(blank=True)
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Thread(models.Model):
    project = models.ForeignKey(Project, related_name="threads", on_delete=models.CASCADE)
    title = models.CharField(max_length=180, default="New thread")
    memory_enabled = models.BooleanField(default=False)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class ProjectAccessPath(models.Model):
    MODE_CHOICES = [
        ("read", "Read only"),
        ("write", "Read and write"),
    ]
    project = models.ForeignKey(Project, related_name="access_paths", on_delete=models.CASCADE)
    path = models.CharField(max_length=700)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="read")
    note = models.CharField(max_length=180, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["mode", "path"]
        constraints = [
            models.UniqueConstraint(fields=["project", "path", "mode"], name="unique_project_access_path_mode"),
        ]

    def __str__(self):
        return f"{self.project}: {self.mode} {self.path}"


class Message(models.Model):
    ROLE_CHOICES = [
        ("system", "System"),
        ("user", "User"),
        ("assistant", "Assistant"),
        ("tool", "Tool"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("streaming", "Streaming"),
        ("complete", "Complete"),
        ("error", "Error"),
    ]
    thread = models.ForeignKey(Thread, related_name="messages", on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="complete")
    openai_response_id = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"


class AppSetting(models.Model):
    key = models.CharField(max_length=120, unique=True)
    value = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class FeatureFlag(models.Model):
    name = models.CharField(max_length=120, unique=True)
    enabled = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Automation(models.Model):
    STATUS_CHOICES = [
        ("paused", "Paused"),
        ("active", "Active"),
    ]
    name = models.CharField(max_length=140)
    schedule = models.CharField(max_length=180, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="paused")
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ApprovalRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]
    thread = models.ForeignKey(Thread, related_name="approval_requests", on_delete=models.CASCADE)
    command = models.TextField()
    rationale = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.status}: {self.command[:40]}"


class AgentRun(models.Model):
    STATUS_CHOICES = [
        ("running", "Running"),
        ("complete", "Complete"),
        ("error", "Error"),
    ]
    thread = models.ForeignKey(Thread, related_name="agent_runs", on_delete=models.CASCADE)
    user_message = models.ForeignKey(
        Message,
        related_name="agent_runs_as_user",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    assistant_message = models.ForeignKey(
        Message,
        related_name="agent_runs_as_assistant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    attempt = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running")
    goal = models.TextField()
    evaluation_criteria = models.JSONField(default=list, blank=True)
    initial_plan_summary = models.TextField(blank=True)
    current_plan_queue = models.JSONField(default=list, blank=True)
    plan_history = models.JSONField(default=list, blank=True)
    replan_history = models.JSONField(default=list, blank=True)
    final_message = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.status}: {self.goal[:40]}"


class AgentWorkerRun(models.Model):
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("complete", "Complete"),
        ("error", "Error"),
    ]
    run = models.ForeignKey(AgentRun, related_name="worker_runs", on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    role = models.CharField(max_length=40)
    purpose = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    result = models.TextField(blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.name}: {self.status}"


class AgentTaskRecord(models.Model):
    STATUS_CHOICES = [
        ("ok", "OK"),
        ("error", "Error"),
    ]
    run = models.ForeignKey(AgentRun, related_name="task_records", on_delete=models.CASCADE)
    worker = models.ForeignKey(
        AgentWorkerRun,
        related_name="task_records",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    sequence = models.PositiveIntegerField()
    tool = models.CharField(max_length=40)
    purpose = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    input_before = models.TextField(blank=True)
    input_after = models.TextField(blank=True)
    result = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="unique_agent_task_sequence"),
        ]

    def __str__(self):
        return f"{self.run_id}:{self.sequence} {self.tool} {self.status}"

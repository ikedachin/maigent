import ast
import concurrent.futures
import json
import logging
import math
import queue
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from django.db import close_old_connections
from django.db import transaction
from django.db.models import Max
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST

from .access import is_path_allowed
from .access import normalize_access_path
from .config import RuntimeConfig, load_runtime_config
from .file_broker import allowed_image_mime_type, resolve_project_output_file, write_allowed_binary_file, write_allowed_text_file
from .models import AgentRun, AgentTaskRecord, AgentWorkerRun, AppSetting, ApprovalRequest, Automation, FeatureFlag, Message, Project, ProjectAccessPath, Thread
from .openai_client import complete_response, generate_sandbox_code, stream_response
from .prompt_loader import load_prompt
from .slash_commands import handle_slash_command
from .tooling import (
    AgentPlan,
    AgentPlanStep,
    AgentWorkerResult,
    AgentWorkerSpec,
    SandboxDataset,
    build_agent_evaluation_criteria,
    build_agent_goal,
    build_agent_plan,
    build_agent_worker_specs,
    can_build_sandbox_program,
    evaluation_criterion,
    make_sandbox_dataset,
    run_sandbox,
    sandbox_dataset_manifest,
)

logger = logging.getLogger("agent")

BUILTIN_FEATURE_FLAGS = {
    "file_write": {
        "enabled": True,
        "description": "Allow /write and /append slash commands after write access path validation.",
    },
}
MAX_CONTEXT_FILE_CHARS = 8000
AUTO_CONTEXT_FILE_CHARS = 3000
AUTO_CONTEXT_MAX_FILES = 3
DEFAULT_RAG_TOP_K = 3
MAX_RAG_TOP_K = 10
DEFAULT_FINAL_EVALUATION_MAX_RETRIES = 3
KNOWN_TOOL_NAMES = {"rag", "sandbox", "web_search"}
INTERNAL_PLAN_TOOL_NAMES = {"final"}
RAG_MIN_BM25_SCORE = 0.1
AUTO_CONTEXT_EXTENSIONS = {
    ".csv",
    ".json",
    ".md",
    ".py",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
}
SANDBOX_DATASET_MAX_CHARS = 500_000
SANDBOX_DATASET_EXTENSIONS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".txt": "text",
    ".md": "text",
}


@dataclass(frozen=True)
class RagResult:
    input_text: str
    searched: bool
    has_context: bool
    query: str = ""


@dataclass(frozen=True)
class FinalEvaluationSettings:
    enabled: bool
    max_retries: int


@dataclass(frozen=True)
class LlmRagDecision:
    should_search: bool
    query: str
    reason: str


@dataclass(frozen=True)
class LlmToolPlanDecision:
    steps: list[AgentPlanStep]
    rag_query: str
    reason: str


@dataclass(frozen=True)
class InitialClarification:
    needed: bool
    reason: str
    questions: list[str]


@dataclass
class TaskExecutionRecord:
    task: AgentPlanStep
    ok: bool
    input_before: str
    input_after: str
    result: str
    error: str = ""


@dataclass
class ReplanDecision:
    action: str
    reason: str
    plan_queue: list[AgentPlanStep]
    final_message: str = ""


@dataclass
class AgentState:
    goal: str
    evaluation_criteria: list[str]
    plan_queue: list[AgentPlanStep]
    plan_history: list[str]
    task_history: list[TaskExecutionRecord]
    input_text: str
    run: AgentRun | None = None
    allowed_tools: set[str] | None = None
    final_message: str = ""
    stopped: bool = False

    @classmethod
    def from_plan(cls, plan: AgentPlan, user_text: str, run: AgentRun | None = None, allowed_tools: set[str] | None = None):
        return cls(
            goal=plan.goal,
            evaluation_criteria=list(plan.evaluation_criteria),
            plan_queue=list(plan.steps),
            plan_history=[plan.summary],
            task_history=[],
            input_text=user_text,
            run=run,
            allowed_tools=allowed_tools,
        )

    def queue_summary(self) -> str:
        return _summarize_revised_plan(self.plan_queue, "dynamic queue") if self.plan_queue else "(empty)"


class StoredPlanStep(TypedDict):
    tool: str
    purpose: str


class StoredReplanDecision(TypedDict):
    action: str
    reason: str
    plan_queue: list[StoredPlanStep]
    final_message: str


def _ensure_defaults() -> tuple[Project, Thread]:
    project = Project.objects.filter(is_current=True).first() or Project.objects.first()
    if not project:
        project = Project.objects.create(name="Default Project", path="", description="Local workspace", is_current=True)
    if not project.is_current:
        Project.objects.update(is_current=False)
        project.is_current = True
        project.save(update_fields=["is_current", "updated_at"])
    thread = project.threads.first() or Thread.objects.create(project=project, title="Main thread")
    return project, thread


def _ensure_builtin_feature_flags() -> None:
    for name, defaults in BUILTIN_FEATURE_FLAGS.items():
        FeatureFlag.objects.get_or_create(name=name, defaults=defaults)


def dashboard(request, thread_id=None):
    project, thread = _ensure_defaults()
    _ensure_builtin_feature_flags()
    if thread_id:
        thread = get_object_or_404(Thread, id=thread_id)
        project = thread.project
    config = load_runtime_config(project.path)
    rag_top_k = _get_rag_top_k()
    final_evaluation = _get_final_evaluation_settings(config)
    return render(
        request,
        "agent/dashboard.html",
        {
            "projects": Project.objects.all(),
            "current_project": project,
            "threads": Thread.objects.filter(project=project),
            "thread": thread,
            "messages": thread.messages.all(),
            "feature_flags": FeatureFlag.objects.all(),
            "automations": Automation.objects.all(),
            "approvals": ApprovalRequest.objects.filter(thread=thread),
            "access_paths": project.access_paths.all(),
            "config": config,
            "config_values": config.redacted(),
            "rag_top_k": rag_top_k,
            "final_evaluation": final_evaluation,
            "tool_settings": _tool_settings(config),
        },
    )


@require_POST
def create_project(request):
    name = request.POST.get("name", "").strip() or "Untitled Project"
    path = request.POST.get("path", "").strip()
    description = request.POST.get("description", "").strip()
    with transaction.atomic():
        Project.objects.update(is_current=False)
        project = Project.objects.create(name=name, path=path, description=description, is_current=True)
        thread = Thread.objects.create(project=project, title="Main thread")
    return redirect("thread", thread_id=thread.id)


@require_POST
def update_rag_settings(request):
    value = request.POST.get("rag_top_k", str(DEFAULT_RAG_TOP_K)).strip()
    try:
        top_k = int(value)
    except ValueError:
        top_k = DEFAULT_RAG_TOP_K
    top_k = max(1, min(MAX_RAG_TOP_K, top_k))
    AppSetting.objects.update_or_create(key="rag_top_k", defaults={"value": str(top_k)})
    enabled = "final_evaluation_enabled" in request.POST
    retry_value = request.POST.get("final_evaluation_max_retries", str(DEFAULT_FINAL_EVALUATION_MAX_RETRIES)).strip()
    try:
        max_retries = int(retry_value)
    except ValueError:
        max_retries = DEFAULT_FINAL_EVALUATION_MAX_RETRIES
    max_retries = max(0, min(3, max_retries))
    AppSetting.objects.update_or_create(key="final_evaluation_enabled", defaults={"value": "true" if enabled else "false"})
    AppSetting.objects.update_or_create(key="final_evaluation_max_retries", defaults={"value": str(max_retries)})
    return redirect(request.POST.get("next") or "dashboard")


@require_POST
def update_output_path(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    path = request.POST.get("output_path", "").strip()
    project.output_path = normalize_access_path(path) if path else ""
    project.save(update_fields=["output_path", "updated_at"])
    thread = project.threads.first() or Thread.objects.create(project=project, title="Main thread")
    return redirect("thread", thread_id=thread.id)


def serve_artifact_image(request, project_id, relative_path):
    project = get_object_or_404(Project, id=project_id)
    mime_type = allowed_image_mime_type(relative_path)
    if not mime_type:
        raise Http404("artifact image not found")
    resolved, error = resolve_project_output_file(project, relative_path)
    if error or resolved is None or not resolved.exists() or not resolved.is_file():
        raise Http404("artifact image not found")
    response = FileResponse(resolved.open("rb"), content_type=mime_type)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def update_feature_flag(request, flag_id):
    flag = get_object_or_404(FeatureFlag, id=flag_id)
    action = request.POST.get("action")
    if action == "enable":
        flag.enabled = True
    elif action == "disable":
        flag.enabled = False
    else:
        raise Http404("Unknown feature flag action")
    flag.save(update_fields=["enabled", "updated_at"])
    return redirect(request.POST.get("next") or "dashboard")


def browse_directories(request):
    requested = request.GET.get("path", "").strip()
    base = Path(requested).expanduser() if requested else Path.cwd()
    try:
        current = base.resolve()
    except OSError:
        return JsonResponse({"error": "Invalid path"}, status=400)
    if not current.exists() or not current.is_dir():
        return JsonResponse({"error": "Directory not found"}, status=404)

    directories = []
    try:
        for child in current.iterdir():
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    directories.append(
                        {
                            "name": child.name,
                            "path": str(child.resolve()),
                            "is_repo": (child / ".git").exists(),
                        }
                    )
            except OSError:
                continue
    except OSError as exc:
        return JsonResponse({"error": str(exc)}, status=403)

    directories.sort(key=lambda item: item["name"].lower())
    parent = current.parent if current.parent != current else None
    return JsonResponse(
        {
            "current": str(current),
            "parent": str(parent) if parent else "",
            "is_repo": (current / ".git").exists(),
            "directories": directories,
        }
    )


@require_POST
def switch_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    with transaction.atomic():
        Project.objects.update(is_current=False)
        project.is_current = True
        project.save(update_fields=["is_current", "updated_at"])
        thread = project.threads.first() or Thread.objects.create(project=project, title="Main thread")
    return redirect("thread", thread_id=thread.id)


@require_POST
def create_thread(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    title = request.POST.get("title", "").strip() or "New thread"
    thread = Thread.objects.create(project=project, title=title)
    return redirect("thread", thread_id=thread.id)


@require_POST
def add_access_path(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    path = request.POST.get("path", "").strip()
    mode = request.POST.get("mode", "read").strip()
    note = request.POST.get("note", "").strip()
    if mode not in {"read", "write"}:
        raise Http404("Unknown access mode")
    if path:
        ProjectAccessPath.objects.get_or_create(
            project=project,
            path=normalize_access_path(path),
            mode=mode,
            defaults={"note": note},
        )
    thread = project.threads.first() or Thread.objects.create(project=project, title="Main thread")
    return redirect("thread", thread_id=thread.id)


@require_POST
def delete_access_path(request, access_path_id):
    access_path = get_object_or_404(ProjectAccessPath, id=access_path_id)
    project = access_path.project
    access_path.delete()
    thread = project.threads.first() or Thread.objects.create(project=project, title="Main thread")
    return redirect("thread", thread_id=thread.id)


@require_POST
def delete_thread(request, thread_id):
    thread = get_object_or_404(Thread, id=thread_id)
    project = thread.project
    thread.delete()
    next_thread = project.threads.order_by("-updated_at").first()
    if not next_thread:
        next_thread = Thread.objects.create(project=project, title="Main thread")
    return redirect("thread", thread_id=next_thread.id)


@require_POST
def approval_action(request, approval_id):
    approval = get_object_or_404(ApprovalRequest, id=approval_id)
    action = request.POST.get("action")
    if action == "approve":
        approval.status = "approved"
    elif action == "reject":
        approval.status = "rejected"
    else:
        raise Http404("Unknown approval action")
    approval.save(update_fields=["status", "updated_at"])
    return redirect("thread", thread_id=approval.thread_id)


@require_POST
def create_approval(request, thread_id):
    thread = get_object_or_404(Thread, id=thread_id)
    command = request.POST.get("command", "").strip()
    if command:
        ApprovalRequest.objects.create(
            thread=thread,
            command=command,
            rationale=request.POST.get("rationale", "").strip(),
        )
    return redirect("thread", thread_id=thread.id)


@require_POST
def send_message(request, thread_id):
    thread = get_object_or_404(Thread, id=thread_id)
    text = request.POST.get("message", "").strip()
    if not text:
        return JsonResponse({"error": "message is required"}, status=400)

    user_message = Message.objects.create(thread=thread, role="user", content=text)
    config = load_runtime_config(thread.project.path)
    logger.debug(
        "message_received thread_id=%s project=%s chars=%s preview=%r",
        thread.id,
        thread.project.name,
        len(text),
        text[:160],
    )

    if text.startswith("/"):
        content = handle_slash_command(thread, text, config)
        assistant = Message.objects.create(thread=thread, role="assistant", content=content, status="complete")
        return JsonResponse(
            {
                "user_id": user_message.id,
                "assistant_id": assistant.id,
                "content": content,
                "command": True,
                "thread_summary": thread.summary,
            }
        )

    if not config.model:
        content = "モデルが未設定です。config.tomlに model または default_model を設定してください。"
        assistant = Message.objects.create(thread=thread, role="assistant", content=content, status="error")
        return JsonResponse({"user_id": user_message.id, "assistant_id": assistant.id, "content": content, "error": True}, status=400)

    assistant = Message.objects.create(thread=thread, role="assistant", content="", status="pending")
    return JsonResponse(
        {
            "user_id": user_message.id,
            "assistant_id": assistant.id,
            "stream_url": reverse("stream_message", args=[thread.id, assistant.id]),
        }
    )


def stream_message(request, thread_id, message_id):
    thread = get_object_or_404(Thread, id=thread_id)
    assistant = get_object_or_404(Message, id=message_id, thread=thread, role="assistant")
    latest_user = thread.messages.filter(role="user").order_by("-created_at").first()
    if not latest_user:
        return JsonResponse({"error": "no user message"}, status=400)

    config = load_runtime_config(thread.project.path)
    conversation_input = _build_thread_conversation_input(thread, latest_user, assistant)

    def events():
        started_at = time.monotonic()
        progress_lines: list[str] = []

        def emit_progress(message: str):
            progress_lines.append(message)
            return {
                "progress": message,
                "progress_tail": progress_lines[-3:],
                "progress_truncated": len(progress_lines) > 3,
            }

        assistant.status = "streaming"
        assistant.save(update_fields=["status"])
        try:
            instructions = _build_instructions(thread)
            result = yield from _generate_with_final_evaluation(
                thread,
                latest_user.content,
                conversation_input,
                config,
                instructions,
                emit_progress,
                user_message=latest_user,
                assistant_message=assistant,
            )
            assistant.content = result["content"]
            assistant.status = result["status"]
            assistant.openai_response_id = result["response_id"]
            assistant.save(update_fields=["content", "status", "openai_response_id"])
            logger.debug(
                "llm_done thread_id=%s assistant_id=%s output_chars=%s response_id=%s",
                thread.id,
                assistant.id,
                len(assistant.content),
                assistant.openai_response_id or "",
            )
            _log_tail("final_answer", assistant.content, config=config, thread_id=thread.id, assistant_id=assistant.id)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            yield f"data: {json.dumps({'delta': assistant.content})}\n\n"
            yield f"data: {json.dumps({'done': True, 'message_id': assistant.id, 'elapsed_ms': elapsed_ms})}\n\n"
        except Exception as exc:
            assistant.content = str(exc)
            assistant.status = "error"
            assistant.save(update_fields=["content", "status"])
            logger.exception("agent_error thread_id=%s assistant_id=%s", thread.id, assistant.id)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            yield f"data: {json.dumps({'error': str(exc), 'elapsed_ms': elapsed_ms})}\n\n"

    response = StreamingHttpResponse(events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    return response


def _build_instructions(thread: Thread) -> str:
    lines = [load_prompt("base_instructions.txt")]
    if thread.memory_enabled and thread.summary:
        lines.append("Thread memory summary:")
        lines.append(thread.summary)
    return "\n".join(lines)


def _build_thread_conversation_input(thread: Thread, latest_user: Message, assistant_message: Message | None = None) -> str:
    messages = []
    for message in thread.messages.order_by("created_at"):
        if assistant_message and message.id == assistant_message.id:
            continue
        if message.id == latest_user.id:
            continue
        if message.role == "assistant" and message.status in {"pending", "streaming"} and not message.content.strip():
            continue
        content = message.content.strip()
        if not content:
            continue
        messages.append(f"{message.role}:\n{content}")
    history = "\n\n".join(messages)
    return (
        "Thread conversation history:\n"
        f"{history}\n\n"
        "Answer the latest user message using the full conversation history above.\n"
        "Latest user message:\n"
        f"{latest_user.content}"
    )


def _format_sandbox_message(ok: bool, output: str) -> str:
    status = "成功" if ok else "失敗"
    output = _strip_sandbox_artifact_payloads(output).strip()
    if ok and not output:
        output = "成果物を生成しました。"
    return f"Sandbox実行結果: {status}\n\n```text\n{output}\n```"


def _strip_sandbox_artifact_payloads(output: str) -> str:
    output = _strip_marked_sandbox_artifact_payloads(output)

    def strip_json_block(match):
        block = match.group(1).strip()
        return "" if _is_sandbox_artifact_payload(block) else match.group(0)

    text = re.sub(r"```json\s*(.*?)```", strip_json_block, output, flags=re.DOTALL | re.IGNORECASE)
    lines = []
    for line in text.splitlines():
        if _is_sandbox_artifact_payload(line.strip()):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_marked_sandbox_artifact_payloads(output: str) -> str:
    marker = "<MAIGENT_ARTIFACT>"
    text = output
    while marker in text:
        marker_index = text.find(marker)
        object_start = text.find("{", marker_index + len(marker))
        if object_start < 0:
            break
        decoder = json.JSONDecoder()
        try:
            payload, object_end = decoder.raw_decode(text[object_start:])
        except json.JSONDecodeError:
            break
        if not (isinstance(payload, dict) and isinstance(payload.get("maigent_artifacts"), list)):
            break
        text = text[:marker_index] + text[object_start + object_end :]
    return text


def _is_sandbox_artifact_payload(text: str) -> bool:
    if not text or ("maigent_artifacts" not in text and "maigent_sandbox_result" not in text):
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("maigent_artifacts"), list):
        return True
    result = payload.get("maigent_sandbox_result")
    return isinstance(result, dict) and isinstance(result.get("artifacts"), list)


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _log_tail(label: str, text: object, config=None, **fields: object) -> None:
    value = str(text or "")
    tail_chars = _llm_log_tail_chars(config)
    preview = value if tail_chars is None else value[-tail_chars:]
    metadata = " ".join(f"{key}={value!r}" for key, value in fields.items())
    if metadata:
        metadata = " " + metadata
    logger.debug("%s_tail chars=%s tail=%r%s", label, len(value), preview, metadata)


def _llm_log_tail_chars(config) -> int | None:
    if config is None:
        return 100
    value = getattr(config, "llm_log_tail_chars", 100)
    return value if value is None or isinstance(value, int) else 100


def _generate_with_final_evaluation(
    thread: Thread,
    user_text: str,
    input_text: str,
    config,
    instructions: str,
    progress=None,
    user_message: Message | None = None,
    assistant_message: Message | None = None,
):
    settings = _get_final_evaluation_settings(config)
    logger.debug(
        "final_evaluation_settings thread_id=%s enabled=%s max_retries=%s",
        thread.id,
        settings.enabled,
        settings.max_retries,
    )
    attempts = settings.max_retries + 1 if settings.enabled else 1
    last_content = ""
    last_status = "complete"
    last_response_id = ""
    last_reason = ""
    failed_plans: list[str] = []
    retry_feedback: list[str] = []
    for attempt in range(1, attempts + 1):
        logger.debug(
            "agent_attempt_start thread_id=%s attempt=%s/%s failed_plans=%s retry_feedback=%s",
            thread.id,
            attempt,
            attempts,
            failed_plans,
            retry_feedback[-3:],
        )
        if progress:
            yield _sse(progress(f"Attempt {attempt}/{attempts}: planning response path."))
        result = yield from _generate_once(
            thread,
            user_text,
            input_text,
            config,
            instructions,
            attempt,
            failed_plans,
            retry_feedback,
            progress,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        last_content = result["content"]
        last_status = result["status"]
        last_response_id = result["response_id"]
        if not settings.enabled:
            logger.debug("final_evaluation_skipped thread_id=%s reason=disabled", thread.id)
            logger.debug(
                "agent_attempt_done thread_id=%s attempt=%s status=%s plan=%r reason=final_evaluation_disabled",
                thread.id,
                attempt,
                last_status,
                result["plan_summary"],
            )
            return result
        if last_status == "error":
            logger.debug("final_evaluation_skipped thread_id=%s reason=assistant_error", thread.id)
            logger.debug(
                "agent_attempt_done thread_id=%s attempt=%s status=%s plan=%r reason=assistant_error",
                thread.id,
                attempt,
                last_status,
                result["plan_summary"],
            )
            return result
        if result.get("clarification_requested"):
            logger.debug(
                "final_evaluation_skipped thread_id=%s reason=clarification_requested plan=%r",
                thread.id,
                result["plan_summary"],
            )
            return result
        if progress:
            yield _sse(progress(f"Attempt {attempt}: evaluating answer completeness."))
        goal = str(result.get("goal") or build_agent_goal(user_text))
        evaluation_criteria = list(result.get("evaluation_criteria") or build_agent_evaluation_criteria(user_text))
        evaluation_context = str(result.get("evaluation_context") or input_text)
        evaluation = _evaluate_final_answer(config, evaluation_context, goal, evaluation_criteria, last_content)
        logger.debug(
            "final_evaluation_result thread_id=%s attempt=%s goal=%r criteria=%s adequate=%s reason=%r",
            thread.id,
            attempt,
            goal,
            evaluation_criteria,
            evaluation["adequate"],
            str(evaluation["reason"])[:240],
        )
        if evaluation["adequate"]:
            if progress:
                yield _sse(progress(f"Attempt {attempt}: final evaluation passed."))
            logger.debug(
                "agent_attempt_done thread_id=%s attempt=%s status=%s plan=%r final_evaluation=passed reason=%r",
                thread.id,
                attempt,
                last_status,
                result["plan_summary"],
                str(evaluation["reason"])[:240],
            )
            return result
        if evaluation.get("evaluation_failed"):
            if progress:
                yield _sse(progress(f"Attempt {attempt}: final evaluation could not be completed; keeping current answer."))
            logger.debug(
                "agent_attempt_done thread_id=%s attempt=%s status=%s plan=%r final_evaluation=failed_to_evaluate reason=%r next_action=keep_current_answer",
                thread.id,
                attempt,
                last_status,
                result["plan_summary"],
                str(evaluation["reason"])[:240],
            )
            return result
        last_reason = evaluation["reason"]
        failed_plans.append(result["plan_summary"])
        retry_feedback.append(str(last_reason))
        if progress:
            yield _sse(progress(f"Attempt {attempt}: final evaluation failed; replanning with a different plan."))
        logger.debug(
            "final_evaluation_retry thread_id=%s attempt=%s max_retries=%s reason=%r",
            thread.id,
            attempt,
            settings.max_retries,
            last_reason[:240],
        )
        logger.debug(
            "agent_attempt_done thread_id=%s attempt=%s status=%s plan=%r final_evaluation=failed reason=%r next_action=replan",
            thread.id,
            attempt,
            last_status,
            result["plan_summary"],
            last_reason[:240],
        )
    warning = "十分に回答できているかの最終評価を通過できませんでした。"
    if last_reason:
        warning += f"\n評価理由: {last_reason}"
    return {
        "content": f"{last_content}\n\n{warning}",
        "status": last_status,
        "response_id": last_response_id,
    }


def _generate_once(
    thread: Thread,
    user_text: str,
    input_text: str,
    config,
    instructions: str,
    attempt: int,
    failed_plans: list[str],
    retry_feedback: list[str],
    progress=None,
    user_message: Message | None = None,
    assistant_message: Message | None = None,
):
    clarification = _request_initial_clarification_if_needed(config, input_text, latest_user_text=user_text)
    if clarification:
        content = _format_initial_clarification_message(clarification)
        if progress:
            yield _sse(progress("Clarification: asking for missing information before planning."))
        logger.debug(
            "initial_clarification_requested thread_id=%s attempt=%s reason=%r questions=%s",
            thread.id,
            attempt,
            clarification.reason[:240],
            clarification.questions,
        )
        return {
            "content": content,
            "status": "complete",
            "response_id": "",
            "goal": build_agent_goal(user_text),
            "evaluation_criteria": build_agent_evaluation_criteria(user_text),
            "plan_summary": "Clarification requested before planning.",
            "clarification_requested": True,
            "evaluation_context": input_text,
        }
    plan = _build_agent_plan_with_llm_tool_selection(thread, user_text, config)
    if not plan:
        plan = build_agent_plan(user_text, config)
        plan = _apply_llm_rag_decision(thread, user_text, config, plan)
    plan = _avoid_failed_plan(plan, config, failed_plans)
    allowed_tools = _allowed_plan_tools(config)
    plan = _filter_plan_to_allowed_tools(plan, allowed_tools)
    if progress:
        yield _sse(progress(f"Goal: {plan.goal}"))
        yield _sse(progress("Criteria: " + "; ".join(plan.evaluation_criteria)))
        yield _sse(progress(f"Plan: {plan.summary}"))
    logger.debug(
        "agent_plan thread_id=%s attempt=%s goal=%r criteria=%s summary=%s steps=%s",
        thread.id,
        attempt,
        plan.goal,
        plan.evaluation_criteria,
        plan.summary,
        [f"{step.tool}:{step.purpose}" for step in plan.steps],
    )
    run = _create_agent_run(thread, user_message, assistant_message, attempt, plan, allowed_tools)
    try:
        if _multi_agent_enabled(config):
            plan_result = yield from _execute_multi_agent_plan(thread, user_text, config, plan, progress, run=run, input_text=input_text)
        else:
            plan_result = yield from _execute_agent_plan(thread, user_text, config, plan, progress, run=run, input_text=input_text)
    except Exception as exc:
        _finish_agent_run(run, "error", error=str(exc))
        raise
    if plan_result["final_message"]:
        _finish_agent_run(run, "complete" if plan_result["ok"] else "error", str(plan_result["final_message"]))
        return {
            "content": str(plan_result["final_message"]),
            "status": "complete" if plan_result["ok"] else "error",
            "response_id": "",
            "goal": plan.goal,
            "evaluation_criteria": plan.evaluation_criteria,
            "plan_summary": plan.summary,
            "evaluation_context": str(plan_result["input_text"]),
        }

    input_text = str(plan_result["input_text"])
    if retry_feedback:
        input_text = _prepend_retry_feedback(input_text, retry_feedback)
    content_parts: list[str] = []
    response_id = ""
    logger.debug("llm_start thread_id=%s attempt=%s input_chars=%s", thread.id, attempt, len(input_text))
    _log_tail("llm_prompt", input_text, config=config, thread_id=thread.id, attempt=attempt, purpose="answer_generation")
    if progress:
        yield _sse(progress("LLM: generating candidate answer."))
    try:
        for kind, payload in stream_response(config, input_text, instructions):
            if kind == "delta":
                content_parts.append(payload)
            elif kind == "response_id":
                response_id = payload
    except Exception as exc:
        _finish_agent_run(run, "error", error=str(exc))
        raise
    final_content = "".join(content_parts)
    artifact_message = _persist_final_answer_artifact(thread, user_text, final_content)
    if artifact_message:
        final_content = f"{final_content}\n\n{artifact_message}"
    _finish_agent_run(run, "complete", final_content)
    return {
        "content": final_content,
        "status": "complete",
        "response_id": response_id,
        "goal": plan.goal,
        "evaluation_criteria": plan.evaluation_criteria,
        "plan_summary": plan.summary,
        "evaluation_context": input_text,
    }


def _avoid_failed_plan(plan: AgentPlan, config, failed_plans: list[str]) -> AgentPlan:
    if plan.summary not in failed_plans:
        logger.debug("agent_plan_selection base_plan=%r selected=%r reason=not_previously_failed", plan.summary, plan.summary)
        return plan
    candidates: list[AgentPlan] = []
    tool_names = [step.tool for step in plan.steps]
    if "sandbox" in tool_names:
        revised_steps = [step for step in plan.steps if step.tool != "sandbox"]
        if not revised_steps:
            revised_steps = [AgentPlanStep("final", "Answer directly with the language model using final-evaluation feedback.")]
        candidates.append(
            AgentPlan(
                goal=plan.goal,
                evaluation_criteria=plan.evaluation_criteria,
                summary=_summarize_revised_plan(revised_steps, "without sandbox"),
                steps=revised_steps,
                rag_query=plan.rag_query,
            )
        )
    if "rag" not in tool_names and _tool_enabled(config, "rag", default=True):
        revised_steps = [AgentPlanStep("rag", "Search local context to address the final-evaluation failure."), *plan.steps]
        candidates.append(
            AgentPlan(
                goal=plan.goal,
                evaluation_criteria=plan.evaluation_criteria,
                summary=_summarize_revised_plan(revised_steps, "with added rag"),
                steps=revised_steps,
                rag_query=plan.rag_query,
            )
        )
    for candidate in candidates:
        if candidate.summary not in failed_plans:
            logger.debug(
                "agent_plan_selection base_plan=%r selected=%r reason=avoid_failed_plan failed_plans=%s",
                plan.summary,
                candidate.summary,
                failed_plans,
            )
            return candidate
    fallback = AgentPlan(
        goal=plan.goal,
        evaluation_criteria=plan.evaluation_criteria,
        summary=f"Revised direct answer after failed final evaluation ({len(failed_plans) + 1}).",
        steps=[AgentPlanStep("final", "Answer directly while addressing the final-evaluation feedback.")],
        rag_query=plan.rag_query,
    )
    logger.debug(
        "agent_plan_selection base_plan=%r selected=%r reason=fallback_revised_direct_answer failed_plans=%s",
        plan.summary,
        fallback.summary,
        failed_plans,
    )
    return fallback


def _filter_plan_to_allowed_tools(plan: AgentPlan, allowed_tools: set[str]) -> AgentPlan:
    steps = [step for step in plan.steps if step.tool in allowed_tools]
    if not steps:
        steps = [AgentPlanStep("final", "Answer directly with the language model.")]
    if steps == plan.steps:
        return plan
    return AgentPlan(
        goal=plan.goal,
        evaluation_criteria=plan.evaluation_criteria,
        summary=_summarize_revised_plan(steps, "allowed tools"),
        steps=steps,
        rag_query=plan.rag_query,
    )


def _build_agent_plan_with_llm_tool_selection(thread: Thread, user_text: str, config) -> AgentPlan | None:
    if not _tool_selector_llm_enabled(config):
        return None
    tools = _available_tool_specs(thread, config)
    if not tools:
        return None
    decision = _select_tools_with_llm(config, user_text, tools)
    if not decision or not decision.steps:
        return None
    decision = _correct_tool_selection_for_local_context(thread, user_text, tools, decision)
    goal = build_agent_goal(user_text)
    criteria = build_agent_evaluation_criteria(user_text)
    tool_names = [step.tool for step in decision.steps]
    if "rag" in tool_names:
        rag_criterion = evaluation_criterion("rag_selected")
        if rag_criterion not in criteria:
            criteria.append(rag_criterion)
    if "sandbox" in tool_names:
        sandbox_criterion = evaluation_criterion("sandbox")
        if sandbox_criterion not in criteria:
            criteria.append(sandbox_criterion)
    plan = AgentPlan(
        goal=goal,
        evaluation_criteria=criteria,
        summary=_summarize_revised_plan(decision.steps, "LLM-selected tools"),
        steps=decision.steps,
        rag_query=decision.rag_query,
    )
    logger.debug(
        "agent_tool_selection thread_id=%s reason=%r tools=%s rag_query=%r",
        thread.id,
        decision.reason[:240],
        [step.tool for step in plan.steps],
        decision.rag_query,
    )
    return plan


def _correct_tool_selection_for_local_context(
    thread: Thread,
    user_text: str,
    tools: list[dict[str, str]],
    decision: LlmToolPlanDecision,
) -> LlmToolPlanDecision:
    if any(step.tool == "rag" for step in decision.steps):
        return decision
    available = {tool["name"] for tool in tools}
    if "rag" not in available or not _has_allowed_context_sources(thread):
        return decision
    if not _should_force_rag_for_local_context(user_text):
        return decision
    steps = [AgentPlanStep("rag", "Search allowed local files for relevant local context."), *decision.steps]
    rag_query = decision.rag_query.strip() or _build_local_context_rag_query(user_text)
    reason = f"{decision.reason}; corrected to include RAG for likely local/project-specific context."
    logger.debug(
        "tool_selection_corrected thread_id=%s action=prepend_rag original_tools=%s corrected_tools=%s rag_query=%r",
        thread.id,
        [step.tool for step in decision.steps],
        [step.tool for step in steps],
        rag_query,
    )
    return LlmToolPlanDecision(steps=steps, rag_query=rag_query, reason=reason)


def _should_force_rag_for_local_context(user_text: str) -> bool:
    if _should_search(user_text):
        return True
    text = user_text.strip()
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in ["who is", "what is", "tell me about", "について", "教えて"]):
        return bool(_search_terms(text))
    return False


def _build_local_context_rag_query(user_text: str) -> str:
    match = re.search(r"(.+?)(?:について|を教えて|教えて|とは|って何)", user_text)
    if match:
        candidate = match.group(1).strip(" 　、。?？")
        if candidate:
            terms = _search_terms(candidate.replace("の", " "))
            if terms:
                return " ".join(terms)
    return _build_answer_query(user_text)


def _request_initial_clarification_if_needed(config, user_text: str, latest_user_text: str = "") -> InitialClarification | None:
    if not _initial_clarifier_llm_enabled(config):
        return None
    if _should_skip_initial_clarifier(latest_user_text or user_text, config):
        logger.debug("initial_clarifier_skipped reason=actionable_tool_plan")
        return None
    decision = _decide_initial_clarification(config, user_text)
    if not decision or not decision.needed or not decision.questions:
        return None
    return decision


def _should_skip_initial_clarifier(user_text: str, config) -> bool:
    text = user_text.strip()
    if not text:
        return False
    plan = build_agent_plan(text, config)
    planned_tools = {step.tool for step in plan.steps}
    actionable_tools = {"rag", "sandbox", "web_search"}
    return bool(planned_tools & actionable_tools)


def _decide_initial_clarification(config, user_text: str) -> InitialClarification | None:
    instructions = load_prompt("initial_clarifier_instructions.txt")
    prompt = load_prompt("initial_clarifier_prompt.txt", user_text=user_text)
    _log_tail("llm_prompt", prompt, config=config, purpose="initial_clarifier")
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="initial_clarifier",
        config_name="initial_clarifier",
        max_output_tokens=_initial_clarifier_max_output_tokens(config),
        reasoning_effort=_initial_clarifier_reasoning_effort(config),
        log_exceptions=False,
    )
    if not raw:
        return None
    _log_tail("initial_clarifier_raw", raw, config=config)
    try:
        payload = json.loads(_extract_json_object(raw))
    except Exception:
        logger.debug("initial_clarifier_parse_fallback raw=%r", raw[:240])
        return None
    questions_value = payload.get("questions", [])
    if not isinstance(questions_value, list):
        return None
    questions = [str(question).strip() for question in questions_value if str(question).strip()][:3]
    return InitialClarification(
        needed=bool(payload.get("needs_clarification")),
        reason=str(payload.get("reason") or "").strip(),
        questions=questions,
    )


def _format_initial_clarification_message(clarification: InitialClarification) -> str:
    reason = clarification.reason or "実行前に確認が必要です。"
    questions = "\n".join(f"{index}. {question}" for index, question in enumerate(clarification.questions[:3], start=1))
    return f"{reason}\n\n{questions}" if questions else reason


def _select_tools_with_llm(config, user_text: str, tools: list[dict[str, str]]) -> LlmToolPlanDecision | None:
    instructions = _append_allowed_tool_instruction(load_prompt("tool_selection_instructions.txt"), _allowed_plan_tools(config))
    prompt = load_prompt("tool_selection_prompt.txt", user_text=user_text, tools=json.dumps(tools, ensure_ascii=False))
    _log_tail("llm_prompt", prompt, config=config, purpose="tool_selection")
    payload = None
    max_attempts = _tool_selector_max_retries(config) + 1
    for attempt in range(1, max_attempts + 1):
        raw = _complete_response_with_retries(
            config,
            prompt,
            instructions,
            purpose="tool_selection",
            config_name="tool_selector",
            max_output_tokens=_tool_selector_max_output_tokens(config),
            reasoning_effort=_tool_selector_reasoning_effort(config),
            max_retries=0,
            log_exceptions=True,
            temperature=0,
        )
        if not raw:
            logger.debug("tool_selection_empty_response attempt=%s/%s", attempt, max_attempts)
            continue
        _log_tail("tool_selection_raw", raw, config=config)
        try:
            payload = json.loads(_extract_json_object(raw))
            break
        except Exception:
            logger.debug("tool_selection_parse_retry attempt=%s/%s raw=%r", attempt, max_attempts, raw[:240])
            continue
    if payload is None:
        logger.debug("tool_selection_fallback reason=no_valid_response attempts=%s", max_attempts)
        return None
    steps = _parse_plan_tasks(payload.get("steps"), _allowed_plan_tools(config))
    if not steps:
        return None
    allowed = {tool["name"] for tool in tools}
    steps = [step for step in steps if step.tool in allowed]
    if not steps:
        return None
    if steps[-1].tool != "final":
        steps.append(AgentPlanStep("final", "Answer with the available context and tool results."))
    return LlmToolPlanDecision(
        steps=steps,
        rag_query=str(payload.get("rag_query") or "").strip(),
        reason=str(payload.get("reason") or "LLM-selected tool plan"),
    )


def _available_tool_specs(thread: Thread, config) -> list[dict[str, str]]:
    tools = [{"name": "final", "description": "Answer directly with the language model; use when no external context or code execution is needed."}]
    if _tool_enabled(config, "rag", default=True) and _has_allowed_context_sources(thread):
        tools.append({"name": "rag", "description": "Search allowed local files/folders for relevant context before answering."})
    if _tool_enabled(config, "sandbox"):
        tools.append({"name": "sandbox", "description": "Run deterministic Python in Docker for calculations, code execution, data processing, or artifact generation."})
    if _tool_enabled(config, "web_search"):
        tools.append({"name": "web_search", "description": "Collect current or external web information. Currently returns an unimplemented notice."})
    return tools


def _allowed_configured_tool_names(config) -> set[str]:
    names = getattr(config, "enabled_tool_names", None)
    if isinstance(names, set):
        return {str(name) for name in names if str(name) in KNOWN_TOOL_NAMES}
    if isinstance(names, (list, tuple)):
        return {str(name) for name in names if str(name) in KNOWN_TOOL_NAMES}
    if isinstance(config, RuntimeConfig):
        return set()
    tools = getattr(config, "tools", None)
    tool_enabled = getattr(config, "tool_enabled", None)
    if not callable(tool_enabled) and not isinstance(tools, dict):
        return set(KNOWN_TOOL_NAMES)
    enabled: set[str] = set()
    saw_bool = False
    for name in KNOWN_TOOL_NAMES:
        try:
            value = tool_enabled(name, default=False) if callable(tool_enabled) else None
        except Exception:
            value = None
        if isinstance(value, bool):
            saw_bool = True
            if value:
                enabled.add(name)
    if saw_bool:
        return enabled
    if not isinstance(tools, dict):
        return set(KNOWN_TOOL_NAMES)
    return {name for name in KNOWN_TOOL_NAMES if _tool_enabled(config, name)}


def _allowed_plan_tools(config) -> set[str]:
    return _allowed_configured_tool_names(config) | INTERNAL_PLAN_TOOL_NAMES


def _append_allowed_tool_instruction(instructions: str, allowed_tools: set[str]) -> str:
    allowed = ", ".join(sorted(allowed_tools))
    return f"{instructions.rstrip()}\n\nAllowed tools for this run: {allowed}.\nDo not return any tool outside this set."


def _tool_selector_llm_enabled(config) -> bool:
    return _tool_enabled(config, "tool_selector", default=isinstance(config, RuntimeConfig))


def _initial_clarifier_llm_enabled(config) -> bool:
    return _tool_enabled(config, "initial_clarifier", default=isinstance(config, RuntimeConfig))


def _initial_clarifier_max_output_tokens(config) -> int:
    value = _control_config(config, "initial_clarifier").get("max_output_tokens", 8192)
    try:
        return max(32, int(value))
    except (TypeError, ValueError):
        return 8192


def _initial_clarifier_reasoning_effort(config) -> str:
    return _control_config_reasoning_effort(config, "initial_clarifier", "reasoning_effort", "none")


def _tool_selector_max_output_tokens(config) -> int:
    return _control_config_int(config, "tool_selector", "max_output_tokens", 160, minimum=32, maximum=2048)


def _tool_selector_reasoning_effort(config) -> str:
    return _control_config_reasoning_effort(config, "tool_selector", "reasoning_effort", "none")


def _tool_selector_max_retries(config) -> int:
    return _control_config_int(config, "tool_selector", "max_retries", 1, minimum=0, maximum=5)


def _apply_llm_rag_decision(thread: Thread, user_text: str, config, plan: AgentPlan) -> AgentPlan:
    if not _should_ask_llm_for_rag_decision(thread, user_text, config, plan):
        return plan
    decision = _decide_rag_with_llm(config, user_text)
    logger.debug(
        "rag_llm_decision thread_id=%s should_search=%s query=%r reason=%r",
        thread.id,
        decision.should_search,
        decision.query,
        decision.reason[:240],
    )
    if not decision.should_search:
        return plan
    criteria = list(plan.evaluation_criteria)
    rag_criterion = "If local file context may contain the answer, the answer must use RAG context or clearly state that allowed files do not contain enough information."
    if rag_criterion not in criteria:
        criteria.append(rag_criterion)
    return AgentPlan(
        goal=plan.goal,
        evaluation_criteria=criteria,
        summary="rag -> final (LLM-selected)",
        steps=[AgentPlanStep("rag", "Search allowed local files because an LLM judged local context may be needed.")],
        rag_query=decision.query,
    )


def _should_ask_llm_for_rag_decision(thread: Thread, user_text: str, config, plan: AgentPlan) -> bool:
    if not _tool_enabled(config, "rag", default=True):
        return False
    if any(step.tool == "rag" for step in plan.steps):
        return False
    if plan.steps != [AgentPlanStep("final", "Answer directly with the language model.")]:
        return False
    if not _has_allowed_context_sources(thread):
        return False
    text = user_text.strip()
    if len(text) < 8:
        return False
    lowered = text.lower()
    casual = {"hello", "hi", "こんにちは", "ありがとう", "thanks"}
    if lowered in casual or text in casual:
        return False
    return "?" in text or "？" in text or any(marker in text for marker in ["ですか", "ますか", "とは", "について", "どのよう"])


def _has_allowed_context_sources(thread: Thread) -> bool:
    for access in thread.project.access_paths.filter(mode__in=["read", "write"]):
        try:
            path = Path(access.path).expanduser().resolve()
        except OSError:
            continue
        if is_path_allowed(thread.project, str(path), write=False) and path.exists():
            return True
    return False


def _decide_rag_with_llm(config, user_text: str) -> LlmRagDecision:
    instructions = load_prompt("rag_decision_instructions.txt")
    prompt = load_prompt("rag_decision_prompt.txt", user_text=user_text)
    _log_tail("llm_prompt", prompt, config=config, purpose="rag_decision")
    raw = _complete_response_with_retries(config, prompt, instructions, purpose="rag_decision", config_name="rag_decision")
    if not raw:
        return LlmRagDecision(False, "", "decision failed: empty LLM response")
    _log_tail("rag_decision_raw", raw, config=config)
    value = _text_value(raw)
    normalized = value["value"].strip()
    upper = normalized.upper()
    should_search = "RAG_REQUIRED" in upper and "NO_RAG" not in upper.splitlines()[0][:40]
    query = _extract_labeled_value(normalized, "QUERY") or _build_answer_query(user_text)
    reason = _extract_labeled_value(normalized, "REASON") or normalized[:240]
    return LlmRagDecision(should_search=should_search, query=query, reason=reason)


def _summarize_revised_plan(steps: list[AgentPlanStep], suffix: str) -> str:
    names = [step.tool for step in steps]
    if names == ["final"]:
        return f"Revised direct answer ({suffix})."
    summary_names = names if names and names[-1] == "final" else [*names, "final"]
    return " -> ".join(summary_names) + f" ({suffix})"


def _tool_enabled(config, name: str, default: bool = False) -> bool:
    try:
        value = config.tool_enabled(name, default=default)
    except AttributeError:
        return default
    return value if isinstance(value, bool) else default


def _prepend_retry_feedback(input_text: str, retry_feedback: list[str]) -> str:
    feedback = "\n".join(f"- {reason}" for reason in retry_feedback[-3:])
    prefix = load_prompt("retry_feedback_prefix.txt", feedback=feedback)
    return f"{prefix}\n\n{input_text}"


def _evaluate_final_answer(config, evaluation_context: str, goal: str, evaluation_criteria: list[str], answer: str) -> dict[str, object]:
    instructions = load_prompt("final_evaluation_instructions.txt")
    prompt = load_prompt(
        "final_evaluation_prompt.txt",
        evaluation_context=evaluation_context,
        goal=goal,
        evaluation_criteria="\n".join(f"- {criterion}" for criterion in evaluation_criteria),
        answer=answer,
    )
    _log_tail("llm_prompt", prompt, config=config, purpose="final_evaluation")
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="final_evaluation",
        config_name="final_evaluation",
        max_output_tokens=_final_evaluation_max_output_tokens(config),
        reasoning_effort=_final_evaluation_reasoning_effort(config),
    )
    if not raw:
        return {"adequate": False, "reason": "評価に失敗しました: LLMから空の応答が返りました。", "evaluation_failed": True}
    _log_tail("final_evaluation_raw", raw, config=config)
    normalized = _text_value(raw)["value"]
    try:
        payload = json.loads(_extract_json_object(normalized))
        return {"adequate": bool(payload.get("adequate")), "reason": str(payload.get("reason") or "")}
    except Exception:
        if normalized.lstrip().startswith("{") or "adequate" in normalized.lower():
            logger.debug("final_evaluation_parse_failed raw=%r", normalized[:240])
            return {"adequate": False, "reason": "評価結果のJSONが不完全または不正でした。", "evaluation_failed": True}
        upper = normalized.upper()
        if re.search(r"\b(INADEQUATE|FAIL|FAILED)\b", upper):
            return {"adequate": False, "reason": _extract_labeled_value(normalized, "REASON") or normalized[:240]}
        if re.search(r"\b(ADEQUATE|PASS|PASSED)\b", upper):
            return {"adequate": True, "reason": _extract_labeled_value(normalized, "REASON") or normalized[:240]}
        logger.debug("final_evaluation_parse_fallback raw=%r", normalized[:240])
        return {"adequate": False, "reason": "評価結果を解釈できませんでした。", "evaluation_failed": True}


def _text_value(text: str) -> dict[str, str]:
    return {"value": str(text or "")}


def _complete_response_with_retries(
    config,
    prompt: str,
    instructions: str,
    *,
    purpose: str,
    config_name: str,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    log_exceptions: bool = True,
) -> str:
    retries = _llm_response_max_retries(config, config_name) if max_retries is None else max_retries
    attempts = max(1, retries + 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = complete_response(
                config,
                prompt,
                instructions,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
            )
        except Exception as exc:
            last_error = str(exc)
            if log_exceptions:
                logger.exception("%s_error attempt=%s/%s", purpose, attempt, attempts)
            else:
                logger.debug("%s_error attempt=%s/%s error=%r", purpose, attempt, attempts, last_error[:240])
            continue
        raw = str(response or "").strip()
        if raw:
            if attempt > 1:
                logger.debug("%s_retry_succeeded attempt=%s/%s", purpose, attempt, attempts)
            return raw
        logger.debug("%s_empty_response attempt=%s/%s", purpose, attempt, attempts)
    if last_error:
        logger.debug("%s_failed_after_retries attempts=%s last_error=%r", purpose, attempts, last_error[:240])
    else:
        logger.debug("%s_failed_after_retries attempts=%s reason=empty_response", purpose, attempts)
    return ""


def _llm_response_max_retries(config, config_name: str, default: int = 1) -> int:
    value = _config_mapping_int(config, config_name, "llm_max_retries", None)
    if value is None:
        value = _config_mapping_int(config, config_name, "response_max_retries", None)
    if value is None and config_name != "final_evaluation":
        value = _config_mapping_int(config, config_name, "max_retries", None)
    if value is None:
        value = _config_mapping_int(config, "llm", "max_retries", default)
    return max(0, min(5, value if isinstance(value, int) else default))


def _config_mapping(config, name: str) -> dict:
    if name in {"initial_clarifier", "tool_selector", "dynamic_replanner", "dynamic_finalizer"}:
        return _control_config(config, name)
    try:
        value = getattr(config, name, {})
    except AttributeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _config_mapping_int(config, name: str, key: str, default: int | None) -> int | None:
    try:
        value = _config_mapping(config, name).get(key, default)
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _final_evaluation_max_output_tokens(config) -> int:
    value = getattr(config, "final_evaluation_max_output_tokens", 8192)
    return value if isinstance(value, int) and value > 0 else 8192


def _final_evaluation_reasoning_effort(config) -> str:
    value = str(getattr(config, "final_evaluation_reasoning_effort", "none")).strip().lower()
    return value if value in {"none", "minimal", "low", "medium", "high"} else "none"


def _extract_labeled_value(text: str, label: str) -> str:
    pattern = rf"^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$"
    for line in text.splitlines():
        match = re.match(pattern, line, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _parse_integer_list_from_text(text: str) -> list[int]:
    value = _text_value(text)["value"]
    try:
        payload = json.loads(_extract_json_object(value))
        indexes = payload.get("relevant_indexes", [])
        if isinstance(indexes, list):
            return [int(index) for index in indexes]
    except Exception:
        pass
    labeled = _extract_labeled_value(value, "RELEVANT_INDEXES") or _extract_labeled_value(value, "INDEXES")
    if not labeled:
        return []
    return [int(match) for match in re.findall(r"\d+", labeled)]


def _extract_json_object(text: str) -> str:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    return match.group(0) if match else text


def _replan_after_step(config, state: AgentState) -> ReplanDecision:
    if state.final_message:
        return ReplanDecision("finish", "task produced a terminal result", [], state.final_message)
    latest = state.task_history[-1] if state.task_history else None
    if latest and latest.error:
        return ReplanDecision("finish", "task failed with an unrecoverable error", list(state.plan_queue), latest.error)
    if _dynamic_replanner_llm_enabled(config):
        decision = _replan_after_step_with_llm(config, state)
        if decision:
            return decision
    return ReplanDecision("keep", "current queue remains valid", list(state.plan_queue))


def _replan_after_step_with_llm(config, state: AgentState) -> ReplanDecision | None:
    allowed_tools = state.allowed_tools or _allowed_plan_tools(config)
    instructions = _append_allowed_tool_instruction(load_prompt("dynamic_replanner_instructions.txt"), allowed_tools)
    prompt = load_prompt(
        "dynamic_replanner_prompt.txt",
        goal=state.goal,
        evaluation_criteria="\n".join(f"- {criterion}" for criterion in state.evaluation_criteria),
        plan_history="\n".join(f"- {summary}" for summary in state.plan_history),
        task_history=_format_task_history(state),
        plan_queue=_format_plan_queue(state.plan_queue),
    )
    _log_tail("llm_prompt", prompt, config=config, purpose="dynamic_replanner")
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="dynamic_replanner",
        config_name="dynamic_replanner",
        max_output_tokens=_control_config_max_output_tokens(config, "dynamic_replanner"),
        reasoning_effort=_control_config_reasoning_effort(config, "dynamic_replanner", "reasoning_effort", "none"),
    )
    if not raw:
        return None
    _log_tail("dynamic_replanner_raw", raw, config=config)
    try:
        payload = json.loads(_extract_json_object(raw))
    except Exception:
        logger.debug("dynamic_replanner_parse_fallback raw=%r", raw[:240])
        return None
    action = str(payload.get("action") or "keep").strip().lower()
    reason = str(payload.get("reason") or "LLM replanner decision")
    if action == "finish":
        return ReplanDecision("finish", reason, list(state.plan_queue), str(payload.get("final_message") or state.final_message))
    if action == "replace":
        tasks = _parse_plan_tasks(payload.get("tasks"), allowed_tools)
        if tasks:
            return ReplanDecision("replace", reason, tasks)
    return ReplanDecision("keep", reason, list(state.plan_queue))


def _route_final_output(config, state: AgentState) -> ReplanDecision:
    if state.plan_queue:
        return ReplanDecision("keep", "remaining queue still has tasks", list(state.plan_queue))
    if not state.task_history and not state.final_message:
        return ReplanDecision("keep", "final routing: no tool artifacts yet", [])
    if not _dynamic_finalizer_llm_enabled(config):
        action = "discard" if state.final_message else "save"
        return ReplanDecision("keep", f"final routing: {action}", [])
    allowed_tools = state.allowed_tools or _allowed_plan_tools(config)
    instructions = _append_allowed_tool_instruction(load_prompt("dynamic_finalizer_instructions.txt"), allowed_tools)
    prompt = load_prompt(
        "dynamic_finalizer_prompt.txt",
        goal=state.goal,
        task_history=_format_task_history(state),
        artifacts=state.final_message or state.input_text[-4000:],
    )
    _log_tail("llm_prompt", prompt, config=config, purpose="dynamic_finalizer")
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="dynamic_finalizer",
        config_name="dynamic_finalizer",
        max_output_tokens=_control_config_max_output_tokens(config, "dynamic_finalizer"),
        reasoning_effort=_control_config_reasoning_effort(config, "dynamic_finalizer", "reasoning_effort", "none"),
    )
    if not raw:
        return ReplanDecision("keep", "final routing failed; finishing with current output", [])
    _log_tail("dynamic_finalizer_raw", raw, config=config)
    try:
        payload = json.loads(_extract_json_object(raw))
    except Exception:
        return ReplanDecision("keep", "final routing was not parseable; finishing with current output", [])
    action = str(payload.get("action") or "save").strip().lower()
    reason = str(payload.get("reason") or f"final routing: {action}")
    if action == "add_tasks":
        tasks = _parse_plan_tasks(payload.get("tasks"), allowed_tools)
        if tasks:
            return ReplanDecision("replace", reason, tasks)
    return ReplanDecision("keep", reason, [])


def _parse_plan_tasks(raw_tasks: object, allowed_tools: set[str] | None = None) -> list[AgentPlanStep]:
    if not isinstance(raw_tasks, list):
        return []
    allowed = allowed_tools or (KNOWN_TOOL_NAMES | INTERNAL_PLAN_TOOL_NAMES)
    tasks: list[AgentPlanStep] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        if tool in allowed and purpose:
            tasks.append(AgentPlanStep(tool, purpose))
    return tasks


def _format_task_history(state: AgentState) -> str:
    if not state.task_history:
        return "(none)"
    lines = []
    for index, record in enumerate(state.task_history[-10:], start=1):
        status = "ok" if record.ok else "error"
        result = (record.result or record.error or "").replace("\n", " ")[:500]
        lines.append(f"{index}. {record.task.tool}: {status}; purpose={record.task.purpose}; result={result}")
    return "\n".join(lines)


def _format_plan_queue(queue: list[AgentPlanStep]) -> str:
    if not queue:
        return "(empty)"
    return "\n".join(f"{index}. {step.tool}: {step.purpose}" for index, step in enumerate(queue, start=1))


def _dynamic_replanner_llm_enabled(config) -> bool:
    return _tool_enabled(config, "dynamic_replanner", default=isinstance(config, RuntimeConfig))


def _dynamic_finalizer_llm_enabled(config) -> bool:
    return _tool_enabled(config, "dynamic_finalizer", default=isinstance(config, RuntimeConfig))


def _control_config(config, name: str) -> dict:
    try:
        getter = getattr(config, "control_config", None)
        if callable(getter):
            value = getter(name)
            return value if isinstance(value, dict) else {}
        direct = getattr(config, name, {})
        if isinstance(direct, dict):
            return direct
        tools = getattr(config, "tools", {})
        legacy = tools.get(name, {}) if isinstance(tools, dict) else {}
        return legacy if isinstance(legacy, dict) else {}
    except AttributeError:
        return {}


def _control_config_int(config, name: str, key: str, default: int, minimum: int = 1, maximum: int = 1024) -> int:
    try:
        value = _control_config(config, name).get(key, default)
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _control_config_max_output_tokens(config, name: str, default: int = 8192) -> int:
    try:
        value = _control_config(config, name).get("max_output_tokens", default)
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _control_config_reasoning_effort(config, name: str, key: str, default: str = "none") -> str:
    value = _control_config(config, name).get(key, default)
    if isinstance(value, bool):
        return "medium" if value else "none"
    normalized = str(value).strip().lower()
    if normalized in {"true", "on", "yes"}:
        return "medium"
    if normalized in {"false", "off", "no"}:
        return "none"
    return normalized if normalized in {"none", "minimal", "low", "medium", "high"} else default


def _create_agent_run(
    thread: Thread,
    user_message: Message | None,
    assistant_message: Message | None,
    attempt: int,
    plan: AgentPlan,
    allowed_tools: set[str] | None = None,
) -> AgentRun:
    return AgentRun.objects.create(
        thread=thread,
        user_message=user_message,
        assistant_message=assistant_message,
        attempt=attempt,
        status="running",
        goal=plan.goal,
        evaluation_criteria=list(plan.evaluation_criteria),
        initial_plan_summary=plan.summary,
        current_plan_queue=_serialize_plan_queue(plan.steps, allowed_tools),
        plan_history=[plan.summary],
    )


def _sync_agent_run_state(state: AgentState, status: str = "running", final_message: str = "", error: str = "") -> None:
    if not state.run:
        return
    state.run.status = status
    state.run.current_plan_queue = _serialize_plan_queue(state.plan_queue, state.allowed_tools)
    state.run.plan_history = list(state.plan_history)
    if final_message:
        state.run.final_message = _truncate_db_text(final_message)
    if error:
        state.run.error = _truncate_db_text(error)
    state.run.save(update_fields=["status", "current_plan_queue", "plan_history", "final_message", "error", "updated_at"])


def _record_agent_task(state: AgentState, record: TaskExecutionRecord) -> None:
    if not state.run:
        return
    _create_agent_task_record(state.run, record)


def _create_agent_task_record(run: AgentRun, record: TaskExecutionRecord, worker: AgentWorkerRun | None = None) -> None:
    with transaction.atomic():
        sequence = (AgentTaskRecord.objects.filter(run=run).aggregate(value=Max("sequence"))["value"] or 0) + 1
        AgentTaskRecord.objects.create(
            run=run,
            worker=worker,
            sequence=sequence,
            tool=record.task.tool,
            purpose=record.task.purpose,
            status="ok" if record.ok else "error",
            input_before=_truncate_db_text(record.input_before),
            input_after=_truncate_db_text(record.input_after),
            result=_truncate_db_text(record.result),
            error=_truncate_db_text(record.error),
        )


def _record_replan_decision(state: AgentState, decision: ReplanDecision) -> None:
    if not state.run:
        return
    history = list(state.run.replan_history or [])
    history.append(
        {
            "action": decision.action,
            "reason": decision.reason,
            "plan_queue": _serialize_plan_queue(decision.plan_queue, state.allowed_tools),
            "final_message": _truncate_db_text(decision.final_message, limit=2000),
        }
    )
    state.run.replan_history = history
    state.run.save(update_fields=["replan_history", "updated_at"])


def _finish_agent_run(run: AgentRun | None, status: str, final_message: str = "", error: str = "") -> None:
    if not run:
        return
    run.status = status
    if final_message:
        run.final_message = _truncate_db_text(final_message)
    if error:
        run.error = _truncate_db_text(error)
    run.save(update_fields=["status", "final_message", "error", "updated_at"])


def _serialize_plan_queue(queue: list[AgentPlanStep], allowed_tools: set[str] | None = None) -> list[StoredPlanStep]:
    allowed = allowed_tools or (KNOWN_TOOL_NAMES | INTERNAL_PLAN_TOOL_NAMES)
    serialized: list[StoredPlanStep] = []
    for step in queue:
        if step.tool not in allowed:
            continue
        purpose = str(step.purpose or "").strip()
        if not purpose:
            continue
        serialized.append({"tool": step.tool, "purpose": purpose})
    return serialized


def _truncate_db_text(text: object, limit: int = 12000) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[truncated]"


def _multi_agent_enabled(config) -> bool:
    value = getattr(config, "multi_agent_enabled", None)
    if isinstance(value, bool):
        return value
    if isinstance(config, RuntimeConfig):
        return True
    multi_agent = getattr(config, "multi_agent", None)
    if isinstance(multi_agent, dict):
        return _as_bool_like(multi_agent.get("enabled", True))
    return False


def _multi_agent_max_workers(config) -> int:
    value = getattr(config, "multi_agent_max_workers", None)
    if isinstance(value, int):
        return max(1, min(5, value))
    multi_agent = getattr(config, "multi_agent", None)
    if isinstance(multi_agent, dict):
        try:
            return max(1, min(5, int(multi_agent.get("max_workers", 3))))
        except (TypeError, ValueError):
            return 3
    return 3


def _multi_agent_parallel_tools(config) -> bool:
    value = getattr(config, "multi_agent_parallel_tools", None)
    if isinstance(value, bool):
        return value
    multi_agent = getattr(config, "multi_agent", None)
    if isinstance(multi_agent, dict):
        return _as_bool_like(multi_agent.get("parallel_tools", True))
    return True


def _multi_agent_progress_visible(config) -> bool:
    value = getattr(config, "multi_agent_progress_visible", None)
    if isinstance(value, bool):
        return value
    multi_agent = getattr(config, "multi_agent", None)
    if isinstance(multi_agent, dict):
        return _as_bool_like(multi_agent.get("progress_visible", True))
    return True


def _as_bool_like(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _build_multi_agent_workers(plan: AgentPlan, user_text: str, config) -> list[AgentWorkerSpec]:
    return build_agent_worker_specs(
        plan,
        max_workers=_multi_agent_max_workers(config),
        parallel_tools=_multi_agent_parallel_tools(config),
    )


def _emit_agent_progress(agent: str, status: str, message: str, progress=None):
    if not progress:
        return None
    payload = progress(f"{agent}: {message}")
    payload.update(
        {
            "agent": agent,
            "agent_status": status,
            "agent_progress": message,
        }
    )
    return _sse(payload)


def _execute_multi_agent_plan(
    thread: Thread,
    user_text: str,
    config,
    plan: AgentPlan,
    progress=None,
    run: AgentRun | None = None,
    input_text: str | None = None,
):
    workers = _build_multi_agent_workers(plan, user_text, config)
    if len(workers) < 2 or not run:
        return (yield from _execute_agent_plan(thread, user_text, config, plan, progress, run=run, input_text=input_text))

    allowed_tools = _allowed_plan_tools(config)
    plan = _filter_plan_to_allowed_tools(plan, allowed_tools)
    base_input = input_text or user_text
    parent_state = AgentState.from_plan(plan, base_input, run=run, allowed_tools=allowed_tools)
    parent_state.plan_history.append(_summarize_worker_plan(workers))
    parent_state.plan_queue = []
    _sync_agent_run_state(parent_state)

    progress_visible = _multi_agent_progress_visible(config)
    worker_records = _create_agent_worker_runs(run, workers)
    progress_queue: queue.Queue[dict[str, str]] = queue.Queue()
    results: list[AgentWorkerResult] = []

    if progress_visible and progress:
        for spec in workers:
            yield _emit_agent_progress(spec.name, "queued", spec.purpose, progress)

    pending_workers = list(workers)
    worker_inputs = {worker.name: base_input for worker in workers}
    research_dependency = _sandbox_depends_on_research(plan, workers)
    if research_dependency:
        research = next((worker for worker in workers if worker.name == "research"), None)
        if research:
            research_result = yield from _run_worker_round(
                [research],
                worker_inputs,
                thread,
                user_text,
                config,
                plan,
                run,
                worker_records,
                progress_queue,
                progress_visible,
                progress,
            )
            results.extend(research_result)
            pending_workers = [worker for worker in workers if worker.name != "research"]
            if research_result:
                research_input = research_result[0].input_text
                for worker in pending_workers:
                    if worker.name == "compute":
                        worker_inputs[worker.name] = research_input

    results.extend(
        (yield from _run_worker_round(
            pending_workers,
            worker_inputs,
            thread,
            user_text,
            config,
            plan,
            run,
            worker_records,
            progress_queue,
            progress_visible,
            progress,
        ))
    )

    results.sort(key=lambda result: _worker_order(workers, result.name))
    parent_state.task_history = [
        TaskExecutionRecord(
            task=AgentPlanStep(result.role, result.purpose),
            ok=result.ok,
            input_before=base_input,
            input_after=result.input_text,
            result=result.result,
            error=result.error,
        )
        for result in results
    ]
    parent_state.input_text = _format_worker_results_for_synthesis(base_input, plan, results)

    finalization = _route_final_output(config, parent_state)
    _record_replan_decision(parent_state, finalization)
    if finalization.action == "replace" and finalization.plan_queue:
        if progress:
            yield _sse(progress("Final routing: adding validation tasks after worker synthesis."))
        return (yield from _execute_agent_plan(
            thread,
            parent_state.input_text,
            config,
            AgentPlan(
                goal=parent_state.goal,
                evaluation_criteria=parent_state.evaluation_criteria,
                summary=_summarize_revised_plan(finalization.plan_queue, "multi-agent final routing"),
                steps=finalization.plan_queue,
            ),
            progress,
            run=run,
            input_text=parent_state.input_text,
        ))
    if finalization.action == "finish" and finalization.final_message:
        parent_state.final_message = finalization.final_message
    _sync_agent_run_state(parent_state)
    if parent_state.final_message:
        return {
            "ok": not any(result.error for result in results),
            "input_text": parent_state.input_text,
            "final_message": parent_state.final_message,
        }
    return {"ok": True, "input_text": parent_state.input_text, "final_message": ""}


def _run_worker_round(
    workers: list[AgentWorkerSpec],
    worker_inputs: dict[str, str],
    thread: Thread,
    user_text: str,
    config,
    plan: AgentPlan,
    run: AgentRun,
    worker_records: dict[str, AgentWorkerRun],
    progress_queue: queue.Queue[dict[str, str]],
    progress_visible: bool,
    progress=None,
):
    if not workers:
        return []
    results: list[AgentWorkerResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(workers), _multi_agent_max_workers(config))) as executor:
        futures = [
            executor.submit(
                _execute_agent_worker,
                thread,
                user_text,
                config,
                worker_inputs.get(spec.name, user_text),
                plan,
                spec,
                worker_records.get(spec.name),
                run,
                progress_queue,
            )
            for spec in workers
        ]
        pending = set(futures)
        while pending:
            while True:
                try:
                    item = progress_queue.get_nowait()
                except queue.Empty:
                    break
                _handle_worker_progress(worker_records, item)
                if progress_visible and progress:
                    yield _emit_agent_progress(item["agent"], item["status"], item["message"], progress)
            done, pending = concurrent.futures.wait(pending, timeout=0.05, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                result = future.result()
                results.append(result)
                _store_worker_result(run, worker_records.get(result.name), result)
        while True:
            try:
                item = progress_queue.get_nowait()
            except queue.Empty:
                break
            _handle_worker_progress(worker_records, item)
            if progress_visible and progress:
                yield _emit_agent_progress(item["agent"], item["status"], item["message"], progress)
    return results


def _sandbox_depends_on_research(plan: AgentPlan, workers: list[AgentWorkerSpec]) -> bool:
    names = {worker.name for worker in workers}
    if not {"research", "compute"} <= names:
        return False
    tools = [step.tool for step in plan.steps]
    try:
        return tools.index("rag") < tools.index("sandbox")
    except ValueError:
        return False


def _execute_agent_worker(
    thread: Thread,
    user_text: str,
    config,
    input_text: str,
    plan: AgentPlan,
    spec: AgentWorkerSpec,
    worker_run: AgentWorkerRun | None,
    run: AgentRun,
    progress_queue: queue.Queue[dict[str, str]],
) -> AgentWorkerResult:
    close_old_connections()
    try:
        progress_queue.put({"agent": spec.name, "status": "running", "message": spec.purpose})
        if not spec.steps:
            result = AgentWorkerResult(
                name=spec.name,
                role=spec.role,
                purpose=spec.purpose,
                ok=True,
                input_text=input_text,
                result="Ready to review worker outputs during synthesis.",
            )
            progress_queue.put({"agent": spec.name, "status": "complete", "message": "ready for synthesis"})
            return result

        worker_input = input_text
        final_message = ""
        ok = True
        result_parts: list[str] = []
        task_records: list[dict[str, str]] = []
        plan_trace = [
            f"Agent worker: {spec.name}",
            f"Worker role: {spec.role}",
            f"Worker purpose: {spec.purpose}",
            f"Parent goal: {plan.goal}",
        ]

        def worker_progress(message: str) -> dict[str, object]:
            progress_queue.put({"agent": spec.name, "status": "running", "message": message})
            return {}

        for step in spec.steps:
            input_before = worker_input
            outcome = _consume_generator(
                _execute_agent_task(thread, user_text, config, worker_input, list(plan_trace), step, plan.rag_query, worker_progress)
            )
            worker_input = str(outcome["input_text"])
            final_message = str(outcome["final_message"])
            step_ok = bool(outcome["ok"])
            ok = ok and step_ok
            result_parts.append(final_message or f"{step.tool} completed")
            task_records.append(
                {
                    "tool": step.tool,
                    "purpose": step.purpose,
                    "status": "ok" if step_ok else "error",
                    "input_before": input_before,
                    "input_after": worker_input,
                    "result": final_message or "completed",
                    "error": "" if step_ok else final_message,
                }
            )
        result_text = "\n\n".join(part for part in result_parts if part).strip() or "completed"
        error = "" if ok else result_text
        progress_queue.put({"agent": spec.name, "status": "complete" if ok else "error", "message": result_text[:240]})
        return AgentWorkerResult(spec.name, spec.role, spec.purpose, ok, worker_input, result_text, error, tuple(task_records))
    except Exception as exc:
        error = str(exc)
        progress_queue.put({"agent": spec.name, "status": "error", "message": error[:240]})
        return AgentWorkerResult(spec.name, spec.role, spec.purpose, False, input_text, "", error)
    finally:
        close_old_connections()


def _consume_generator(generator):
    while True:
        try:
            next(generator)
        except StopIteration as exc:
            return exc.value


def _create_agent_worker_runs(run: AgentRun, specs: list[AgentWorkerSpec]) -> dict[str, AgentWorkerRun]:
    records: dict[str, AgentWorkerRun] = {}
    for spec in specs:
        records[spec.name] = AgentWorkerRun.objects.create(
            run=run,
            name=spec.name,
            role=spec.role,
            purpose=spec.purpose,
            status="queued",
        )
    return records


def _handle_worker_progress(worker_records: dict[str, AgentWorkerRun], item: dict[str, str]) -> None:
    status = item.get("status", "")
    if status != "running":
        return
    worker = worker_records.get(item.get("agent", ""))
    if worker and worker.status == "queued":
        _update_worker_run(worker, "running", started=True)


def _store_worker_result(run: AgentRun, worker: AgentWorkerRun | None, result: AgentWorkerResult) -> None:
    _update_worker_run(
        worker,
        "complete" if result.ok else "error",
        result=result.result if result.ok else "",
        error=result.error,
        finished=True,
    )
    for item in result.task_records:
        _create_agent_task_record(
            run,
            TaskExecutionRecord(
                task=AgentPlanStep(str(item.get("tool", "")), str(item.get("purpose", ""))),
                ok=item.get("status") == "ok",
                input_before=str(item.get("input_before", "")),
                input_after=str(item.get("input_after", "")),
                result=str(item.get("result", "")),
                error=str(item.get("error", "")),
            ),
            worker=worker,
        )


def _update_worker_run(
    worker: AgentWorkerRun | None,
    status: str,
    *,
    result: str = "",
    error: str = "",
    started: bool = False,
    finished: bool = False,
) -> None:
    if not worker:
        return
    fields = ["status", "updated_at"]
    worker.status = status
    if result:
        worker.result = _truncate_db_text(result)
        fields.append("result")
    if error:
        worker.error = _truncate_db_text(error)
        fields.append("error")
    now = timezone.now()
    if started:
        worker.started_at = now
        fields.append("started_at")
    if finished:
        worker.finished_at = now
        fields.append("finished_at")
    worker.save(update_fields=fields)


def _format_worker_results_for_synthesis(input_text: str, plan: AgentPlan, results: list[AgentWorkerResult]) -> str:
    instructions = load_prompt("multi_agent_synthesis_instructions.txt")
    lines = [
        instructions,
        "",
        "Original request and conversation context:",
        input_text,
        "",
        "Multi-agent goal:",
        plan.goal,
        "",
        "Evaluation criteria:",
        *[f"- {criterion}" for criterion in plan.evaluation_criteria],
        "",
        "Worker results:",
    ]
    for result in results:
        status = "ok" if result.ok else "error"
        body = result.result or result.error or "(no result)"
        lines.append(f"\n[{result.name}] role={result.role}; status={status}; purpose={result.purpose}\n{body}")
    return "\n".join(lines)


def _summarize_worker_plan(workers: list[AgentWorkerSpec]) -> str:
    return "multi-agent: " + ", ".join(f"{worker.name}({worker.role})" for worker in workers)


def _worker_order(workers: list[AgentWorkerSpec], name: str) -> int:
    for index, worker in enumerate(workers):
        if worker.name == name:
            return index
    return len(workers)


def _execute_agent_plan(
    thread: Thread,
    user_text: str,
    config,
    plan: AgentPlan,
    progress=None,
    run: AgentRun | None = None,
    input_text: str | None = None,
):
    allowed_tools = _allowed_plan_tools(config)
    plan = _filter_plan_to_allowed_tools(plan, allowed_tools)
    state = AgentState.from_plan(plan, input_text or user_text, run=run, allowed_tools=allowed_tools)
    _sync_agent_run_state(state)
    plan_trace = [
        f"Agent goal: {plan.goal}",
        "Agent evaluation criteria:",
        *[f"- {criterion}" for criterion in plan.evaluation_criteria],
        f"Agent plan: {plan.summary}",
    ]
    while state.plan_queue and not state.stopped:
        step = state.plan_queue.pop(0)
        if step.tool == "final":
            _sync_agent_run_state(state)
            continue
        input_before = state.input_text
        outcome = yield from _execute_agent_task(thread, user_text, config, state.input_text, plan_trace, step, plan.rag_query, progress)
        state.input_text = str(outcome["input_text"])
        state.final_message = str(outcome["final_message"])
        task_record = TaskExecutionRecord(
            task=step,
            ok=bool(outcome["ok"]),
            input_before=input_before,
            input_after=state.input_text,
            result=state.final_message or "completed",
            error="" if outcome["ok"] else state.final_message,
        )
        state.task_history.append(task_record)
        _record_agent_task(state, task_record)
        if progress:
            yield _sse(progress(f"Replanner: evaluating remaining queue after {step.tool}."))
        decision = _replan_after_step(config, state)
        _record_replan_decision(state, decision)
        logger.debug(
            "agent_replan thread_id=%s action=%s reason=%r queue=%s",
            thread.id,
            decision.action,
            decision.reason[:240],
            [f"{task.tool}:{task.purpose}" for task in decision.plan_queue],
        )
        if progress:
            yield _sse(progress(f"Replanner: {decision.action} - {decision.reason}"))
        if decision.action == "finish":
            state.stopped = True
            if decision.final_message:
                state.final_message = decision.final_message
        elif decision.action == "replace":
            state.plan_queue = list(decision.plan_queue)
            state.plan_history.append(_summarize_revised_plan(state.plan_queue, "dynamic replan"))
        else:
            state.plan_queue = list(decision.plan_queue)
        _sync_agent_run_state(state)

    finalization = _route_final_output(config, state)
    _record_replan_decision(state, finalization)
    if finalization.action == "replace" and finalization.plan_queue:
        state.plan_queue = list(finalization.plan_queue)
        state.plan_history.append(_summarize_revised_plan(state.plan_queue, "final output routing"))
        _sync_agent_run_state(state)
        logger.debug("agent_final_route action=add_tasks queue=%s", [step.tool for step in state.plan_queue])
        if progress:
            yield _sse(progress("Final routing: adding validation tasks before output."))
        return (yield from _execute_agent_plan(
            thread,
            state.input_text,
            config,
            AgentPlan(
                goal=state.goal,
                evaluation_criteria=state.evaluation_criteria,
                summary=state.queue_summary(),
                steps=state.plan_queue,
            ),
            progress,
            run=state.run,
            input_text=state.input_text,
        ))
    if state.final_message:
        return {
            "ok": not any(record.error for record in state.task_history),
            "input_text": state.input_text,
            "final_message": state.final_message,
        }
    if plan_trace and plan.steps[0].tool != "final":
        state.input_text = _prepend_plan_trace(state.input_text, plan_trace)
    return {"ok": True, "input_text": state.input_text, "final_message": ""}


def _execute_agent_task(
    thread: Thread,
    user_text: str,
    config,
    input_text: str,
    plan_trace: list[str],
    step: AgentPlanStep,
    preferred_rag_query: str = "",
    progress=None,
):
    logger.debug("agent_step_start thread_id=%s tool=%s purpose=%s", thread.id, step.tool, step.purpose)
    if progress:
        yield _sse(progress(f"Tool {step.tool}: {step.purpose}"))
    if step.tool == "web_search":
        logger.debug("agent_step_result thread_id=%s tool=web_search status=unimplemented", thread.id)
        return {
            "ok": True,
            "input_text": input_text,
            "final_message": "Web検索ツールは計画で選択されましたが、まだ未実装です。",
        }
    if step.tool == "rag":
        rag = _build_rag_input(thread, user_text, preferred_rag_query, input_text)
        if rag.searched and not rag.has_context:
            logger.debug("agent_step_result thread_id=%s tool=rag status=no_context query=%r", thread.id, rag.query)
            if progress:
                yield _sse(progress("Tool rag: no adequate context found."))
            return {
                "ok": True,
                "input_text": input_text,
                "final_message": "許可済みファイル内に、この質問へ回答するための十分な情報が見つかりませんでした。",
            }
        plan_trace.append(f"RAG result: adequate; query={rag.query or '(none)'}")
        logger.debug("agent_step_result thread_id=%s tool=rag status=adequate query=%r", thread.id, rag.query)
        if progress:
            yield _sse(progress(f"Tool rag: context found for query '{rag.query or '(none)'}'."))
        return {"ok": True, "input_text": _prepend_plan_trace(rag.input_text, plan_trace), "final_message": ""}
    if step.tool == "sandbox":
        generated_code = ""
        datasets = _sandbox_datasets_from_rag_context(thread, input_text)
        sandbox_input_text = _append_sandbox_dataset_manifest(input_text, datasets)
        if not can_build_sandbox_program(input_text):
            logger.debug("sandbox_code_generation_start thread_id=%s", thread.id)
            if progress:
                yield _sse(progress("Tool sandbox: generating executable program."))
            _log_tail("llm_prompt", sandbox_input_text, config=config, thread_id=thread.id, purpose="sandbox_code_generation")
            generated_code = _generate_sandbox_code_with_retries(config, sandbox_input_text)
            if not generated_code.strip():
                plan_trace.append("Sandbox result: skipped before execution; no executable program could be generated.")
                logger.debug(
                    "agent_step_result thread_id=%s tool=sandbox status=no_executable_program",
                    thread.id,
                )
                if progress:
                    yield _sse(progress("Tool sandbox: skipped because no executable program was generated."))
                return {
                    "ok": False,
                    "input_text": _prepend_plan_trace(input_text, plan_trace),
                    "final_message": _format_sandbox_message(False, "sandboxで実行するPythonコードを生成できませんでした。"),
                }
            logger.debug(
                "sandbox_code_generation_done thread_id=%s code_preview=%r",
                thread.id,
                generated_code[:240],
            )
            sandbox_input_text = sandbox_input_text + "\n\nGenerated sandbox program:\n```python\n" + generated_code + "\n```"
            input_text = input_text + "\n\nGenerated sandbox program:\n```python\n" + generated_code + "\n```"
        result = run_sandbox(sandbox_input_text, config, code=generated_code, datasets=datasets)
        if generated_code and not _is_sandbox_result_adequate(result.ok, result.output):
            result, generated_code = _retry_generated_sandbox_after_execution_failure(
                config,
                sandbox_input_text,
                generated_code,
                result,
                thread.id,
                datasets,
            )
        if not result.ok and "Pythonコードまたは計算式を特定できませんでした" in result.output:
            plan_trace.append("Sandbox result: skipped; no executable code or numeric data was found.")
            logger.debug(
                "agent_step_result thread_id=%s tool=sandbox status=skipped_no_code",
                thread.id,
            )
            if progress:
                yield _sse(progress("Tool sandbox: skipped because executable code or numeric data was not found."))
            return {
                "ok": False,
                "input_text": _prepend_plan_trace(input_text, plan_trace),
                "final_message": _format_sandbox_message(False, result.output),
            }
        if not _is_sandbox_result_adequate(result.ok, result.output):
            logger.debug(
                "agent_step_result thread_id=%s tool=sandbox status=failed output_preview=%r",
                thread.id,
                result.output[:240],
            )
            if progress:
                yield _sse(progress("Tool sandbox: execution failed or returned inadequate output."))
            return {"ok": False, "input_text": input_text, "final_message": _format_sandbox_message(result.ok, result.output)}
        plan_trace.append("Sandbox result: adequate")
        artifact_message = _persist_sandbox_artifacts(thread, user_text, result.output, result.artifacts, result.raw_output)
        logger.debug(
            "agent_step_result thread_id=%s tool=sandbox status=adequate output_preview=%r",
            thread.id,
            result.output[:240],
        )
        if progress:
            yield _sse(progress("Tool sandbox: execution completed with adequate output."))
        final_message = _format_sandbox_message(True, result.output)
        if artifact_message:
            final_message = f"{final_message}\n\n{artifact_message}"
        return {"ok": True, "input_text": input_text, "final_message": final_message}
    return {"ok": True, "input_text": input_text, "final_message": ""}


def _generate_sandbox_code_with_retries(config, input_text: str) -> str:
    attempts = _llm_response_max_retries(config, "sandbox_code_generation") + 1
    last_error = ""
    prompt = input_text
    for attempt in range(1, attempts + 1):
        try:
            generated_code = str(generate_sandbox_code(config, prompt) or "").strip()
        except Exception as exc:
            last_error = str(exc)
            logger.exception("sandbox_code_generation_error attempt=%s/%s", attempt, attempts)
            continue
        if generated_code:
            policy_error = _sandbox_code_policy_violation(generated_code)
            if policy_error:
                last_error = policy_error
                logger.debug(
                    "sandbox_code_generation_policy_rejected attempt=%s/%s reason=%s code_preview=%r",
                    attempt,
                    attempts,
                    policy_error,
                    generated_code[:240],
                )
                prompt = (
                    input_text
                    + "\n\nPrevious generated code was rejected before execution.\n"
                    + f"Reason: {policy_error}\n"
                    + "Regenerate executable Python that reads only embedded RAG/message text, never local files. "
                    + "When host-provided sandbox datasets are listed, use load_dataset(dataset_id) and do not paste rows "
                    + "into Python string literals. "
                    + "For images, render to io.BytesIO, base64-encode the image bytes, and print one "
                    + "maigent_sandbox_result JSON object with artifacts[].content_base64. Do not call os.path.exists, open, "
                    + "Path.read_text, pandas read_* with file paths, or savefig with a filesystem path.\n"
                )
                continue
            if attempt > 1:
                logger.debug("sandbox_code_generation_retry_succeeded attempt=%s/%s", attempt, attempts)
            return generated_code
        logger.debug("sandbox_code_generation_empty_response attempt=%s/%s", attempt, attempts)
    if last_error:
        logger.debug("sandbox_code_generation_failed_after_retries attempts=%s last_error=%r", attempts, last_error[:240])
    return ""


def _retry_generated_sandbox_after_execution_failure(
    config,
    input_text: str,
    generated_code: str,
    result,
    thread_id: int,
    datasets: tuple[SandboxDataset, ...] = (),
):
    attempts = _llm_response_max_retries(config, "sandbox_code_generation")
    for attempt in range(1, attempts + 1):
        prompt = (
            input_text
            + "\n\nPrevious generated sandbox code failed during execution.\n"
            + "Previous code:\n"
            + "```python\n"
            + generated_code
            + "\n```\n"
            + "Execution output / traceback:\n"
            + "```text\n"
            + result.output[:4000]
            + "\n```\n"
            + "Regenerate corrected executable Python. Fix missing imports and runtime errors. "
            + "Use only embedded RAG/message text, never local files. When host-provided sandbox datasets are listed, "
            + "use load_dataset(dataset_id) and do not paste rows into Python string literals. For images, render to io.BytesIO, "
            + "base64-encode the image bytes, and print one maigent_sandbox_result JSON object with "
            + "artifacts[].content_base64.\n"
        )
        logger.debug(
            "sandbox_code_execution_retry_start thread_id=%s attempt=%s/%s output_preview=%r",
            thread_id,
            attempt,
            attempts,
            result.output[:240],
        )
        repaired_code = _generate_sandbox_code_with_retries(config, prompt)
        if not repaired_code.strip():
            logger.debug(
                "sandbox_code_execution_retry_no_code thread_id=%s attempt=%s/%s",
                thread_id,
                attempt,
                attempts,
            )
            continue
        generated_code = repaired_code
        result = run_sandbox(input_text, config, code=generated_code, datasets=datasets)
        if _is_sandbox_result_adequate(result.ok, result.output):
            logger.debug(
                "sandbox_code_execution_retry_succeeded thread_id=%s attempt=%s/%s",
                thread_id,
                attempt,
                attempts,
            )
            break
    return result, generated_code


def _sandbox_code_policy_violation(code: str) -> str:
    if "maigent_artifacts" in code or "<MAIGENT_ARTIFACT>" in code:
        return "legacy artifact payload format is not allowed; use maigent_sandbox_result"
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    if _contains_embedded_tabular_literal(tree):
        return "embedded tabular data copies are not allowed; use load_dataset(dataset_id)"
    string_assignments = _constant_string_assignments(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in {"open", "Path.open", "Path.read_text", "Path.read_bytes"}:
            return f"local file access is not allowed: {name}"
        if name in {
            "os.path.exists",
            "os.path.isfile",
            "os.path.isdir",
            "Path.exists",
            "Path.is_file",
            "Path.is_dir",
        } and node.args and _string_argument_value(node.args[0], string_assignments) is not None:
            return f"local filesystem checks are not allowed: {name}"
        if name in {
            "pd.read_csv",
            "pandas.read_csv",
            "read_csv",
            "pd.read_table",
            "pandas.read_table",
            "read_table",
            "pd.read_excel",
            "pandas.read_excel",
            "read_excel",
        } and node.args and _string_argument_value(node.args[0], string_assignments) is not None:
            return f"local file reads are not allowed: {name}"
        if name in {"plt.savefig", "matplotlib.pyplot.savefig", "savefig"}:
            if node.args and _string_argument_value(node.args[0], string_assignments) is not None:
                return f"sandbox file writes are not allowed: {name}"
            for keyword in node.keywords:
                if keyword.arg == "fname" and _string_argument_value(keyword.value, string_assignments) is not None:
                    return f"sandbox file writes are not allowed: {name}"
    return ""


def _contains_embedded_tabular_literal(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        lines = [line for line in node.value.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        comma_like_lines = sum(1 for line in lines if "," in line or "，" in line or "\t" in line)
        if comma_like_lines >= 3:
            return True
    return False


def _constant_string_assignments(tree: ast.AST) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                assignments[node.target.id] = node.value.value
    return assignments


def _string_argument_value(node: ast.AST, assignments: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return assignments.get(node.id)
    return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parent = _call_name(func.value)
        return f"{parent}.{func.attr}" if parent else func.attr
    if isinstance(func, ast.Call):
        return _call_name(func.func)
    return ""


def _persist_sandbox_artifacts(
    thread: Thread,
    input_text: str,
    output: str,
    artifacts: tuple[dict[str, object], ...] | list[dict[str, object]] = (),
    raw_output: str = "",
) -> str:
    requests = _artifact_requests_from_items(list(artifacts))
    if not requests:
        requests = _extract_sandbox_artifact_requests(raw_output or output)
    if not requests:
        requests = _implicit_sandbox_artifact_requests(input_text, output)
    if not requests:
        return ""
    lines = ["Sandbox成果物の保存結果:"]
    for request in requests:
        result = _write_artifact_request(thread.project, request)
        prefix = "OK" if result.ok else "NG"
        lines.append(f"- {prefix}: {result.message}")
        image_markdown = _artifact_image_markdown(thread.project, result.path)
        if result.ok and image_markdown:
            lines.append(image_markdown)
    return "\n".join(lines)


def _persist_final_answer_artifact(thread: Thread, input_text: str, output: str) -> str:
    requests = _implicit_sandbox_artifact_requests(input_text, output)
    if not requests:
        return ""
    lines = ["回答の保存結果:"]
    for request in requests[:1]:
        result = _write_artifact_request(thread.project, request)
        prefix = "OK" if result.ok else "NG"
        lines.append(f"- {prefix}: {result.message}")
        image_markdown = _artifact_image_markdown(thread.project, result.path)
        if result.ok and image_markdown:
            lines.append(image_markdown)
    return "\n".join(lines)


def _write_artifact_request(project: Project, request: dict[str, object]):
    if request.get("content_base64") is not None:
        return write_allowed_binary_file(
            project,
            str(request["path"]),
            str(request["content_base64"]),
            append=bool(request.get("append", False)),
        )
    return write_allowed_text_file(
        project,
        str(request["path"]),
        str(request["content"]),
        append=bool(request.get("append", False)),
    )


def _artifact_image_markdown(project: Project, saved_path: str) -> str:
    if not saved_path or not allowed_image_mime_type(saved_path):
        return ""
    output_root = (project.output_path or "").strip()
    if not output_root:
        return ""
    try:
        root = Path(output_root).expanduser().resolve()
        path = Path(saved_path).expanduser().resolve()
    except OSError:
        return ""
    if path != root and root not in path.parents:
        return ""
    relative_path = path.relative_to(root).as_posix()
    url = reverse("artifact_image", args=[project.id, relative_path])
    return f"![{Path(relative_path).name}]({url})"


def _extract_sandbox_artifact_requests(output: str) -> list[dict[str, object]]:
    payloads: list[object] = []
    for block in re.findall(r"```json\s*(.*?)```", output, flags=re.DOTALL | re.IGNORECASE):
        try:
            payloads.append(json.loads(block.strip()))
        except json.JSONDecodeError:
            continue
    try:
        payloads.append(json.loads(_extract_json_object(output)))
    except Exception:
        pass

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        result = payload.get("maigent_sandbox_result")
        if isinstance(result, dict):
            artifacts = result.get("artifacts", [])
        else:
            artifacts = payload.get("maigent_artifacts", [])
        if not isinstance(artifacts, list):
            continue
        requests = _artifact_requests_from_items(artifacts)
        if requests:
            return requests[:5]
    return []


def _artifact_requests_from_items(items: list[object]) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        content = item.get("content")
        content_base64 = item.get("content_base64")
        if not path or (content is None and content_base64 is None):
            continue
        request = {"path": path, "append": bool(item.get("append", False))}
        if content_base64 is not None and allowed_image_mime_type(path):
            request["content_base64"] = str(content_base64)
            request["mime_type"] = str(item.get("mime_type") or "")
        elif content is not None:
            request["content"] = str(content)
        else:
            continue
        requests.append(request)
    return requests[:5]


def _implicit_sandbox_artifact_requests(input_text: str, output: str) -> list[dict[str, object]]:
    request_text = _artifact_request_text(input_text)
    if not _looks_like_save_request(request_text):
        return []
    paths = _extract_candidate_paths(request_text)
    content = output.strip()
    if not content:
        return []
    path = paths[-1] if paths else _default_artifact_filename(request_text)
    return [{"path": path, "content": content + "\n", "append": False}]


def _artifact_request_text(input_text: str) -> str:
    return re.split(r"\n\s*(?:RAG context from allowed local files|Auto-selected file|File):", input_text, maxsplit=1)[0]


def _default_artifact_filename(input_text: str) -> str:
    lowered = input_text.lower()
    if "histogram" in lowered or "ヒストグラム" in input_text:
        return "maigent-histogram.txt"
    if "png" in lowered or "画像" in input_text:
        return "maigent-output.txt"
    if "csv" in lowered and "test.csv" not in lowered:
        return "maigent-output.csv"
    if "json" in lowered:
        return "maigent-output.json"
    if "markdown" in lowered or ".md" in lowered:
        return "maigent-output.md"
    return "maigent-output.txt"


def _looks_like_save_request(text: str) -> bool:
    lowered = text.lower()
    return any(marker in text for marker in ["保存", "書き込", "出力", "作成"]) or any(
        marker in lowered for marker in ["save", "write", "export", "create"]
    )


def _prepend_plan_trace(input_text: str, plan_trace: list[str]) -> str:
    return "\n".join(plan_trace) + "\n\n" + input_text


def _is_sandbox_result_adequate(ok: bool, output: str) -> bool:
    return ok and bool(output.strip()) and "traceback" not in output.lower()


def _tool_settings(config) -> list[dict[str, str]]:
    sandbox_libraries = ", ".join(config.sandbox_allowed_libraries) or "none"
    return [
        {"name": "rag", "enabled": "on" if config.tool_enabled("rag", default=True) else "off", "detail": "BM25 local files"},
        {"name": "web_search", "enabled": "on" if config.tool_enabled("web_search") else "off", "detail": "not implemented"},
        {
            "name": "sandbox",
            "enabled": "on" if config.tool_enabled("sandbox") else "off",
            "detail": f"{config.sandbox_image}; libs: {sandbox_libraries}",
        },
        {
            "name": "initial_clarifier",
            "enabled": "on" if _initial_clarifier_llm_enabled(config) else "off",
            "detail": f"LLM missing-info check; max output: {_initial_clarifier_max_output_tokens(config)}; reasoning: {_initial_clarifier_reasoning_effort(config)}",
        },
        {
            "name": "dynamic_replanner",
            "enabled": "on" if _tool_enabled(config, "dynamic_replanner", default=isinstance(config, RuntimeConfig)) else "off",
            "detail": "LLM queue rewrite after each task",
        },
        {
            "name": "dynamic_finalizer",
            "enabled": "on" if _tool_enabled(config, "dynamic_finalizer", default=isinstance(config, RuntimeConfig)) else "off",
            "detail": "LLM save/discard/add-tasks routing",
        },
        {
            "name": "tool_selector",
            "enabled": "on" if _tool_selector_llm_enabled(config) else "off",
            "detail": f"LLM tool choice; max output: {_tool_selector_max_output_tokens(config)}; reasoning: {_tool_selector_reasoning_effort(config)}",
        },
    ]


def _get_rag_top_k() -> int:
    setting = AppSetting.objects.filter(key="rag_top_k").first()
    if not setting:
        return DEFAULT_RAG_TOP_K
    try:
        value = int(setting.value)
    except ValueError:
        return DEFAULT_RAG_TOP_K
    return max(1, min(MAX_RAG_TOP_K, value))


def _get_final_evaluation_settings(config) -> FinalEvaluationSettings:
    enabled_setting = AppSetting.objects.filter(key="final_evaluation_enabled").first()
    retry_setting = AppSetting.objects.filter(key="final_evaluation_max_retries").first()
    config_enabled = getattr(config, "final_evaluation_enabled", False)
    config_max_retries = getattr(config, "final_evaluation_max_retries", DEFAULT_FINAL_EVALUATION_MAX_RETRIES)
    enabled = config_enabled if isinstance(config_enabled, bool) else False
    max_retries = config_max_retries if isinstance(config_max_retries, int) else DEFAULT_FINAL_EVALUATION_MAX_RETRIES
    if enabled_setting:
        enabled = enabled_setting.value.strip().lower() in {"1", "true", "yes", "on"}
    if retry_setting:
        try:
            max_retries = int(retry_setting.value)
        except ValueError:
            max_retries = DEFAULT_FINAL_EVALUATION_MAX_RETRIES
    return FinalEvaluationSettings(enabled=enabled, max_retries=max(0, min(3, max_retries)))


def _build_rag_input(thread: Thread, user_text: str, preferred_query: str = "", answer_text: str = "") -> RagResult:
    answer_text = answer_text or user_text
    if not preferred_query.strip() and not _should_search(user_text):
        logger.debug("rag_decision thread_id=%s search=false", thread.id)
        return RagResult(input_text=answer_text, searched=False, has_context=False)
    top_k = _get_rag_top_k()
    query = preferred_query.strip() or _build_answer_query(user_text)
    logger.debug("rag_decision thread_id=%s search=true query=%r top_k=%s", thread.id, query, top_k)
    attachments = _collect_allowed_path_context(thread, user_text, top_k=top_k, answer_query=query)
    if not attachments:
        logger.debug("rag_result thread_id=%s status=no_context query=%r", thread.id, query)
        return RagResult(input_text=answer_text, searched=True, has_context=False, query=query)
    logger.debug("rag_result thread_id=%s status=has_context query=%r attachments=%s", thread.id, query, len(attachments))
    input_text = (
        answer_text
        + f"\n\nRAG search query: {query}"
        + "\n\nRAG context from allowed local files:\n"
        + "\n\n".join(attachments)
        + "\n\nUse the RAG context only when it directly supports the answer. If it does not, say that the allowed files do not contain enough information."
    )
    return RagResult(input_text=input_text, searched=True, has_context=True, query=query)


def _append_sandbox_dataset_manifest(input_text: str, datasets: tuple[SandboxDataset, ...]) -> str:
    manifest = sandbox_dataset_manifest(datasets)
    if not manifest:
        return input_text
    return (
        input_text
        + "\n\nHost-provided sandbox dataset API:\n"
        + manifest
        + "\n\nGenerated code must use load_dataset(\"rag_1\") or another listed dataset id for these files. "
        + "Do not paste CSV/TSV/JSON rows into Python string literals."
    )


def _sandbox_datasets_from_rag_context(thread: Thread, input_text: str) -> tuple[SandboxDataset, ...]:
    datasets: list[SandboxDataset] = []
    for path_text in _rag_context_file_paths(input_text):
        if len(datasets) >= 5:
            break
        try:
            path = Path(path_text).expanduser().resolve()
        except OSError:
            continue
        kind = SANDBOX_DATASET_EXTENSIONS.get(path.suffix.lower())
        if not kind or not is_path_allowed(thread.project, str(path), write=False):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        truncated = len(text) > SANDBOX_DATASET_MAX_CHARS
        if truncated:
            text = text[:SANDBOX_DATASET_MAX_CHARS]
        datasets.append(
            make_sandbox_dataset(
                f"rag_{len(datasets) + 1}",
                path.name,
                str(path),
                kind,
                text,
                truncated=truncated,
            )
        )
    if datasets:
        logger.debug(
            "sandbox_datasets_built thread_id=%s datasets=%s",
            thread.id,
            [{"id": item.id, "name": item.name, "kind": item.kind, "rows": item.row_count} for item in datasets],
        )
    return tuple(datasets)


def _rag_context_file_paths(input_text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"^(?:Auto-selected file|File):\s+(.+?)\s*$", input_text, flags=re.MULTILINE):
        path = match.group(1).strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _build_answer_query(text: str) -> str:
    terms = _search_terms(text)
    if terms:
        return " ".join(terms)
    return text.strip()


def _should_search(text: str) -> bool:
    lowered = text.lower()
    if _extract_candidate_paths(text):
        return True
    if re.search(r"[a-z0-9_.-]+\.(?:csv|tsv|txt|md|json|yaml|yml|py|html|xml|pdf|docx|xlsx|pptx)", lowered):
        return True
    japanese_markers = [
        "ファイル",
        "資料",
        "ドキュメント",
        "一覧",
        "リスト",
        "要約",
        "内容",
        "読んで",
        "読み込んで",
        "検索",
        "調べ",
        "確認",
        "どこ",
        "どれ",
    ]
    english_markers = [
        "file",
        "files",
        "folder",
        "directory",
        "document",
        "docs",
        "list",
        "summarize",
        "summary",
        "search",
        "find",
        "look up",
        "read",
        "where",
        "which",
    ]
    return any(marker in text for marker in japanese_markers) or any(marker in lowered for marker in english_markers)


def _collect_allowed_path_context(
    thread: Thread,
    text: str,
    top_k: int = DEFAULT_RAG_TOP_K,
    answer_query: str = "",
) -> list[str]:
    contexts: list[str] = []
    for path_text in _extract_candidate_paths(text):
        if len(contexts) >= 5:
            break
        try:
            path = Path(path_text).expanduser().resolve()
        except OSError:
            continue
        if not is_path_allowed(thread.project, str(path), write=False) or not path.exists():
            continue
        if path.is_file():
            contexts.append(_read_context_file(path))
            logger.debug("rag_attachment explicit_file=%s", path)
        elif path.is_dir():
            contexts.append(_read_context_directory(path))
            logger.debug("rag_attachment explicit_directory=%s", path)
    if not contexts:
        if _looks_like_file_list_request(text):
            contexts.extend(_collect_allowed_directory_listings(thread))
        else:
            contexts.extend(_collect_relevant_allowed_files(thread, answer_query or text, top_k=top_k))
    return [context for context in contexts if context]


def _extract_candidate_paths(text: str) -> list[str]:
    pattern = r"(?:~|/)[^\s\"'<>]+"
    seen: set[str] = set()
    paths: list[str] = []
    for match in re.findall(pattern, text):
        cleaned = _clean_candidate_path(match)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            paths.append(cleaned)
    return paths


def _clean_candidate_path(value: str) -> str:
    cleaned = value.rstrip("。、,.):;]")
    extension_match = re.match(
        r"^(.+\.(?:csv|tsv|txt|md|json|yaml|yml|py|html|xml|pdf|docx|xlsx|pptx|png|jpg|jpeg|svg))(?:[ぁ-んァ-ン一-龥].*)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if extension_match:
        return extension_match.group(1)
    for suffix in ["に保存してください", "へ保存してください", "に保存", "へ保存", "に書き込んで", "へ書き込んで"]:
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


def _looks_like_file_list_request(text: str) -> bool:
    lowered = text.lower()
    japanese_list = any(term in text for term in ["一覧", "リスト", "列挙", "見せて"])
    japanese_target = any(term in text for term in ["ファイル", "フォルダ", "ディレクトリ"])
    english_list = any(term in lowered for term in ["list", "show", "files", "folders", "directory"])
    return (japanese_list and japanese_target) or english_list


def _collect_allowed_directory_listings(thread: Thread) -> list[str]:
    contexts: list[str] = []
    seen: set[Path] = set()
    for access in thread.project.access_paths.filter(mode__in=["read", "write"]):
        if len(contexts) >= 5:
            break
        try:
            path = Path(access.path).expanduser().resolve()
        except OSError:
            continue
        directory = path if path.is_dir() else path.parent
        if directory in seen or not is_path_allowed(thread.project, str(directory), write=False):
            continue
        seen.add(directory)
        if directory.exists() and directory.is_dir():
            contexts.append(_read_context_directory(directory))
    return contexts


def _read_context_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return f"File: {path}\n[Could not read as UTF-8 text: {exc}]"
    truncated = len(content) > MAX_CONTEXT_FILE_CHARS
    if truncated:
        content = content[:MAX_CONTEXT_FILE_CHARS] + "\n[truncated]"
    return f"File: {path}\n```text\n{content}\n```"


def _collect_relevant_allowed_files(thread: Thread, text: str, top_k: int = DEFAULT_RAG_TOP_K) -> list[str]:
    terms = _search_terms(text)
    if not terms:
        return []
    docs = _collect_candidate_documents(thread)
    named_files = _extract_named_file_tokens(text)
    if named_files:
        exact_matches = [path for path, _doc_text in docs if path.name.lower() in named_files]
        if exact_matches:
            logger.debug(
                "rag_named_file_match thread_id=%s names=%s paths=%s",
                thread.id,
                sorted(named_files),
                [str(path) for path in exact_matches[:top_k]],
            )
            return [_read_context_file_limited(path, AUTO_CONTEXT_FILE_CHARS) for path in exact_matches[:top_k]]
    ranked = _rank_bm25(terms, docs)
    logger.debug(
        "bm25_rank thread_id=%s query_terms=%s candidates=%s ranked_top=%s",
        thread.id,
        terms,
        len(docs),
        [(round(score, 4), str(path)) for score, path in ranked[:top_k]],
    )
    if not _is_rag_result_adequate(terms, ranked, docs):
        logger.debug("bm25_adequacy thread_id=%s adequate=false", thread.id)
        return _collect_llm_judged_relevant_files(thread, text, ranked[:top_k], docs)
    logger.debug("bm25_adequacy thread_id=%s adequate=true", thread.id)
    relevant = [(score, path) for score, path in ranked if score >= RAG_MIN_BM25_SCORE]
    return [_read_context_file_limited(path, AUTO_CONTEXT_FILE_CHARS) for score, path in relevant[:top_k]]


def _extract_named_file_tokens(text: str) -> set[str]:
    pattern = r"[a-z0-9_.-]+\.(?:csv|tsv|txt|md|json|yaml|yml|py|html|xml|pdf|docx|xlsx|pptx)"
    return {match.lower() for match in re.findall(pattern, text.lower())}


def _collect_llm_judged_relevant_files(
    thread: Thread,
    query: str,
    ranked_candidates: list[tuple[float, Path]],
    documents: list[tuple[Path, str]],
) -> list[str]:
    if not ranked_candidates:
        return []
    docs_by_path = {path: text for path, text in documents}
    judged_paths = _judge_rag_candidate_paths_with_llm(thread, query, ranked_candidates, docs_by_path)
    if not judged_paths:
        logger.debug("rag_llm_judge thread_id=%s selected=0", thread.id)
        return []
    logger.debug("rag_llm_judge thread_id=%s selected=%s paths=%s", thread.id, len(judged_paths), [str(path) for path in judged_paths])
    return [_read_context_file_limited(path, AUTO_CONTEXT_FILE_CHARS) for path in judged_paths]


def _judge_rag_candidate_paths_with_llm(
    thread: Thread,
    query: str,
    ranked_candidates: list[tuple[float, Path]],
    docs_by_path: dict[Path, str],
) -> list[Path]:
    config = load_runtime_config(thread.project.path)
    snippets: list[str] = []
    candidate_paths: list[Path] = []
    for index, (score, path) in enumerate(ranked_candidates, start=1):
        text = docs_by_path.get(path, "")
        if not text:
            continue
        candidate_paths.append(path)
        snippets.append(
            "\n".join(
                [
                    f"Candidate {index}",
                    f"path: {path}",
                    f"bm25_score: {score:.4f}",
                    "snippet:",
                    text[:2000],
                ]
            )
        )
    if not snippets:
        return []
    instructions = load_prompt("rag_candidate_judge_instructions.txt")
    prompt = load_prompt("rag_candidate_judge_prompt.txt", query=query, candidate_files="\n\n".join(snippets))
    _log_tail("llm_prompt", prompt, config=config, thread_id=thread.id, purpose="rag_candidate_judge")
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="rag_candidate_judge",
        config_name="rag_candidate_judge",
    )
    if not raw:
        return []
    _log_tail("rag_candidate_judge_raw", raw, config=config, thread_id=thread.id)
    indexes = _parse_integer_list_from_text(raw)
    if not indexes:
        return []
    selected: list[Path] = []
    for index in indexes:
        try:
            position = int(index) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= position < len(candidate_paths):
            selected.append(candidate_paths[position])
    return selected


def _is_rag_result_adequate(
    query_terms: list[str],
    ranked: list[tuple[float, Path]],
    documents: list[tuple[Path, str]],
) -> bool:
    if not query_terms or not ranked:
        return False
    top_score, top_path = ranked[0]
    if top_score < RAG_MIN_BM25_SCORE:
        return False
    doc_text = ""
    for path, text in documents:
        if path == top_path:
            doc_text = text.lower()
            break
    matched_terms = [term for term in query_terms if term in doc_text]
    coverage = len(matched_terms) / len(query_terms)
    return coverage >= 0.34


def _collect_candidate_documents(thread: Thread) -> list[tuple[Path, str]]:
    documents: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for access in thread.project.access_paths.filter(mode__in=["read", "write"]):
        try:
            root = Path(access.path).expanduser().resolve()
        except OSError:
            continue
        candidates = [root] if root.is_file() else _iter_context_files(root)
        for path in candidates:
            if path in seen or not is_path_allowed(thread.project, str(path), write=False):
                continue
            seen.add(path)
            try:
                sample = path.read_text(encoding="utf-8")[:12000]
            except (UnicodeDecodeError, OSError):
                continue
            documents.append((path, f"{path.name}\n{sample}"))
    return documents


def _rank_bm25(query_terms: list[str], documents: list[tuple[Path, str]]) -> list[tuple[float, Path]]:
    if not query_terms or not documents:
        return []
    tokenized = [(_tokenize_for_bm25(text), path, text.lower()) for path, text in documents]
    avgdl = max(1, sum(len(tokens) for tokens, _, _ in tokenized) / len(tokenized))
    doc_freq: dict[str, int] = {}
    for tokens, _, text in tokenized:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1
        for term in query_terms:
            if term in text and term not in tokens:
                doc_freq[term] = doc_freq.get(term, 0) + 1

    k1 = 1.5
    b = 0.75
    ranked: list[tuple[float, Path]] = []
    for tokens, path, text in tokenized:
        if not tokens:
            ranked.append((0.0, path))
            continue
        score = 0.0
        length = len(tokens)
        for term in query_terms:
            tf = tokens.count(term)
            if tf == 0 and term in text:
                tf = 1
            if tf == 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log((len(documents) - df + 0.5) / (df + 0.5) + 1)
            denom = tf + k1 * (1 - b + b * length / avgdl)
            score += idf * (tf * (k1 + 1)) / denom
        ranked.append((score, path))
    ranked.sort(key=lambda item: (-item[0], str(item[1]).lower()))
    return ranked


def _tokenize_for_bm25(text: str) -> list[str]:
    return _search_terms(text)


def _iter_context_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files: list[Path] = []
    try:
        for path in root.rglob("*"):
            if len(files) >= 200:
                break
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            if path.is_file() and path.suffix.lower() in AUTO_CONTEXT_EXTENSIONS:
                files.append(path)
    except OSError:
        return files
    return files


def _search_terms(text: str) -> list[str]:
    raw_terms = re.findall(r"[A-Za-z0-9_.-]{3,}|[一-龥ぁ-んァ-ン]{2,}", text.lower())
    stopwords = {
        "この",
        "ファイル",
        "読んで",
        "読み込んで",
        "について",
        "について要約して",
        "を要約して",
        "要約して",
        "確認",
        "して",
        "ください",
        "教えて",
        "the",
        "this",
        "that",
        "read",
        "file",
        "please",
        "一覧",
        "リスト",
        "要約",
    }
    terms = []
    for term in raw_terms:
        expanded = [term]
        if re.search(r"[_.-]", term):
            expanded.extend(part for part in re.split(r"[_.-]+", term) if len(part) >= 3)
        for item in expanded:
            if item not in stopwords and item not in terms:
                terms.append(item)
    return terms[:12]


def _read_context_file_limited(path: Path, limit: int) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return f"File: {path}\n[Could not read as UTF-8 text: {exc}]"
    truncated = len(content) > limit
    if truncated:
        content = content[:limit] + "\n[truncated]"
    return f"Auto-selected file: {path}\n```text\n{content}\n```"


def _read_context_directory(path: Path) -> str:
    try:
        children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:80]
    except OSError as exc:
        return f"Folder: {path}\n[Could not list directory: {exc}]"
    lines = [f"{'[dir]' if child.is_dir() else '[file]'} {child.name}" for child in children]
    return f"Folder: {path}\n" + ("\n".join(lines) if lines else "(empty)")

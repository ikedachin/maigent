import json
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .access import is_path_allowed
from .access import normalize_access_path
from .config import RuntimeConfig, load_runtime_config
from .file_broker import write_allowed_text_file
from .models import AgentRun, AgentTaskRecord, AppSetting, ApprovalRequest, Automation, FeatureFlag, Message, Project, ProjectAccessPath, Thread
from .openai_client import complete_response, generate_sandbox_code, stream_response
from .prompt_loader import load_prompt
from .slash_commands import handle_slash_command
from .tooling import (
    AgentPlan,
    AgentPlanStep,
    build_agent_evaluation_criteria,
    build_agent_goal,
    build_agent_plan,
    can_build_sandbox_program,
    run_sandbox,
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
    final_message: str = ""
    stopped: bool = False

    @classmethod
    def from_plan(cls, plan: AgentPlan, user_text: str, run: AgentRun | None = None):
        return cls(
            goal=plan.goal,
            evaluation_criteria=list(plan.evaluation_criteria),
            plan_queue=list(plan.steps),
            plan_history=[plan.summary],
            task_history=[],
            input_text=user_text,
            run=run,
        )

    def queue_summary(self) -> str:
        return _summarize_revised_plan(self.plan_queue, "dynamic queue") if self.plan_queue else "(empty)"


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
        return JsonResponse({"user_id": user_message.id, "assistant_id": assistant.id, "content": content, "command": True})

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
            _log_tail("final_answer", assistant.content, thread_id=thread.id, assistant_id=assistant.id)
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


def _format_sandbox_message(ok: bool, output: str) -> str:
    status = "成功" if ok else "失敗"
    return f"Sandbox実行結果: {status}\n\n```text\n{output}\n```"


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _log_tail(label: str, text: object, **fields: object) -> None:
    value = str(text or "")
    metadata = " ".join(f"{key}={value!r}" for key, value in fields.items())
    if metadata:
        metadata = " " + metadata
    logger.debug("%s_tail chars=%s tail=%r%s", label, len(value), value[-100:], metadata)


def _generate_with_final_evaluation(
    thread: Thread,
    user_text: str,
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
        if progress:
            yield _sse(progress(f"Attempt {attempt}: evaluating answer completeness."))
        goal = str(result.get("goal") or build_agent_goal(user_text))
        evaluation_criteria = list(result.get("evaluation_criteria") or build_agent_evaluation_criteria(user_text))
        evaluation = _evaluate_final_answer(config, user_text, goal, evaluation_criteria, last_content)
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
    config,
    instructions: str,
    attempt: int,
    failed_plans: list[str],
    retry_feedback: list[str],
    progress=None,
    user_message: Message | None = None,
    assistant_message: Message | None = None,
):
    plan = _build_agent_plan_with_llm_tool_selection(thread, user_text, config)
    if not plan:
        plan = build_agent_plan(user_text, config)
        plan = _apply_llm_rag_decision(thread, user_text, config, plan)
    plan = _avoid_failed_plan(plan, config, failed_plans)
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
    run = _create_agent_run(thread, user_message, assistant_message, attempt, plan)
    try:
        plan_result = yield from _execute_agent_plan(thread, user_text, config, plan, progress, run=run)
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
        }

    input_text = str(plan_result["input_text"])
    if retry_feedback:
        input_text = _prepend_retry_feedback(input_text, retry_feedback)
    content_parts: list[str] = []
    response_id = ""
    logger.debug("llm_start thread_id=%s attempt=%s input_chars=%s", thread.id, attempt, len(input_text))
    _log_tail("llm_prompt", input_text, thread_id=thread.id, attempt=attempt, purpose="answer_generation")
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
    _finish_agent_run(run, "complete", "".join(content_parts))
    return {
        "content": "".join(content_parts),
        "status": "complete",
        "response_id": response_id,
        "goal": plan.goal,
        "evaluation_criteria": plan.evaluation_criteria,
        "plan_summary": plan.summary,
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


def _build_agent_plan_with_llm_tool_selection(thread: Thread, user_text: str, config) -> AgentPlan | None:
    if not _tool_selector_llm_enabled(config):
        return None
    tools = _available_tool_specs(thread, config)
    if not tools:
        return None
    decision = _select_tools_with_llm(config, user_text, tools)
    if not decision or not decision.steps:
        return None
    goal = build_agent_goal(user_text)
    criteria = build_agent_evaluation_criteria(user_text)
    tool_names = [step.tool for step in decision.steps]
    if "rag" in tool_names:
        rag_criterion = "If local file context may contain the answer, the answer must use RAG context or clearly state that allowed files do not contain enough information."
        if rag_criterion not in criteria:
            criteria.append(rag_criterion)
    if "sandbox" in tool_names:
        sandbox_criterion = "If exact computation or code execution is requested, the answer includes the computed result and does not rely on unsupported mental arithmetic."
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


def _select_tools_with_llm(config, user_text: str, tools: list[dict[str, str]]) -> LlmToolPlanDecision | None:
    instructions = load_prompt("tool_selection_instructions.txt")
    prompt = load_prompt("tool_selection_prompt.txt", user_text=user_text, tools=json.dumps(tools, ensure_ascii=False))
    _log_tail("llm_prompt", prompt, purpose="tool_selection")
    payload = None
    max_attempts = _tool_selector_max_retries(config) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            response = complete_response(
                config,
                prompt,
                instructions,
                max_output_tokens=_tool_selector_max_output_tokens(config),
                reasoning_effort=_tool_selector_reasoning_effort(config),
            )
        except Exception:
            logger.exception("tool_selection_error attempt=%s/%s", attempt, max_attempts)
            continue
        raw = str(response or "").strip()
        if not raw:
            logger.debug("tool_selection_empty_response attempt=%s/%s", attempt, max_attempts)
            continue
        _log_tail("tool_selection_raw", raw)
        try:
            payload = json.loads(_extract_json_object(raw))
            break
        except Exception:
            logger.debug("tool_selection_parse_retry attempt=%s/%s raw=%r", attempt, max_attempts, raw[:240])
            continue
    if payload is None:
        logger.debug("tool_selection_fallback reason=no_valid_response attempts=%s", max_attempts)
        return None
    steps = _parse_plan_tasks(payload.get("steps"))
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


def _tool_selector_llm_enabled(config) -> bool:
    return _tool_enabled(config, "tool_selector", default=isinstance(config, RuntimeConfig))


def _tool_selector_max_output_tokens(config) -> int:
    try:
        tools = getattr(config, "tools", {})
        selector = tools.get("tool_selector", {}) if isinstance(tools, dict) else {}
        return max(32, min(512, int(selector.get("max_output_tokens", 160))))
    except (TypeError, ValueError):
        return 160


def _tool_selector_reasoning_effort(config) -> str:
    try:
        tools = getattr(config, "tools", {})
        selector = tools.get("tool_selector", {}) if isinstance(tools, dict) else {}
        return str(selector.get("reasoning_effort", "none") or "none")
    except AttributeError:
        return "none"


def _tool_selector_max_retries(config) -> int:
    try:
        tools = getattr(config, "tools", {})
        selector = tools.get("tool_selector", {}) if isinstance(tools, dict) else {}
        return max(0, int(selector.get("max_retries", 1)))
    except (AttributeError, TypeError, ValueError):
        return 1


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
    _log_tail("llm_prompt", prompt, purpose="rag_decision")
    try:
        raw = complete_response(config, prompt, instructions)
    except Exception as exc:
        logger.exception("rag_llm_decision_error")
        return LlmRagDecision(False, "", f"decision failed: {exc}")
    _log_tail("rag_decision_raw", raw)
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
    return " -> ".join(names) + f" -> final ({suffix})"


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


def _evaluate_final_answer(config, user_text: str, goal: str, evaluation_criteria: list[str], answer: str) -> dict[str, object]:
    instructions = load_prompt("final_evaluation_instructions.txt")
    prompt = load_prompt(
        "final_evaluation_prompt.txt",
        user_text=user_text,
        goal=goal,
        evaluation_criteria="\n".join(f"- {criterion}" for criterion in evaluation_criteria),
        answer=answer,
    )
    _log_tail("llm_prompt", prompt, purpose="final_evaluation")
    try:
        raw = complete_response(
            config,
            prompt,
            instructions,
            max_output_tokens=_final_evaluation_max_output_tokens(config),
            reasoning_effort=_final_evaluation_reasoning_effort(config),
        ).strip()
    except Exception as exc:
        logger.exception("final_evaluation_error")
        return {"adequate": False, "reason": f"評価に失敗しました: {exc}"}
    _log_tail("final_evaluation_raw", raw)
    normalized = _text_value(raw)["value"]
    try:
        payload = json.loads(_extract_json_object(normalized))
        return {"adequate": bool(payload.get("adequate")), "reason": str(payload.get("reason") or "")}
    except Exception:
        upper = normalized.upper()
        if "INADEQUATE" in upper or "FAIL" in upper:
            return {"adequate": False, "reason": _extract_labeled_value(normalized, "REASON") or normalized[:240]}
        if "ADEQUATE" in upper or "PASS" in upper:
            return {"adequate": True, "reason": _extract_labeled_value(normalized, "REASON") or normalized[:240]}
        logger.debug("final_evaluation_parse_fallback raw=%r", normalized[:240])
        return {"adequate": False, "reason": "評価結果を解釈できませんでした。"}


def _text_value(text: str) -> dict[str, str]:
    return {"value": str(text or "")}


def _final_evaluation_max_output_tokens(config) -> int:
    value = getattr(config, "final_evaluation_max_output_tokens", 160)
    return value if isinstance(value, int) and value > 0 else 160


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
        return ReplanDecision("finish", "task produced a terminal result", list(state.plan_queue), state.final_message)
    latest = state.task_history[-1] if state.task_history else None
    if latest and latest.error:
        return ReplanDecision("finish", "task failed with an unrecoverable error", list(state.plan_queue), latest.error)
    if _dynamic_replanner_llm_enabled(config):
        decision = _replan_after_step_with_llm(config, state)
        if decision:
            return decision
    return ReplanDecision("keep", "current queue remains valid", list(state.plan_queue))


def _replan_after_step_with_llm(config, state: AgentState) -> ReplanDecision | None:
    instructions = load_prompt("dynamic_replanner_instructions.txt")
    prompt = load_prompt(
        "dynamic_replanner_prompt.txt",
        goal=state.goal,
        evaluation_criteria="\n".join(f"- {criterion}" for criterion in state.evaluation_criteria),
        plan_history="\n".join(f"- {summary}" for summary in state.plan_history),
        task_history=_format_task_history(state),
        plan_queue=_format_plan_queue(state.plan_queue),
    )
    _log_tail("llm_prompt", prompt, purpose="dynamic_replanner")
    try:
        raw = complete_response(config, prompt, instructions).strip()
    except Exception:
        logger.exception("dynamic_replanner_error")
        return None
    _log_tail("dynamic_replanner_raw", raw)
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
        tasks = _parse_plan_tasks(payload.get("tasks"))
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
    instructions = load_prompt("dynamic_finalizer_instructions.txt")
    prompt = load_prompt(
        "dynamic_finalizer_prompt.txt",
        goal=state.goal,
        task_history=_format_task_history(state),
        artifacts=state.final_message or state.input_text[-4000:],
    )
    _log_tail("llm_prompt", prompt, purpose="dynamic_finalizer")
    try:
        raw = complete_response(config, prompt, instructions).strip()
    except Exception:
        logger.exception("dynamic_finalizer_error")
        return ReplanDecision("keep", "final routing failed; finishing with current output", [])
    _log_tail("dynamic_finalizer_raw", raw)
    try:
        payload = json.loads(_extract_json_object(raw))
    except Exception:
        return ReplanDecision("keep", "final routing was not parseable; finishing with current output", [])
    action = str(payload.get("action") or "save").strip().lower()
    reason = str(payload.get("reason") or f"final routing: {action}")
    if action == "add_tasks":
        tasks = _parse_plan_tasks(payload.get("tasks"))
        if tasks:
            return ReplanDecision("replace", reason, tasks)
    return ReplanDecision("keep", reason, [])


def _parse_plan_tasks(raw_tasks: object) -> list[AgentPlanStep]:
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[AgentPlanStep] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        if tool in {"rag", "sandbox", "web_search", "final"} and purpose:
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


def _create_agent_run(
    thread: Thread,
    user_message: Message | None,
    assistant_message: Message | None,
    attempt: int,
    plan: AgentPlan,
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
        current_plan_queue=_serialize_plan_queue(plan.steps),
        plan_history=[plan.summary],
    )


def _sync_agent_run_state(state: AgentState, status: str = "running", final_message: str = "", error: str = "") -> None:
    if not state.run:
        return
    state.run.status = status
    state.run.current_plan_queue = _serialize_plan_queue(state.plan_queue)
    state.run.plan_history = list(state.plan_history)
    if final_message:
        state.run.final_message = _truncate_db_text(final_message)
    if error:
        state.run.error = _truncate_db_text(error)
    state.run.save(update_fields=["status", "current_plan_queue", "plan_history", "final_message", "error", "updated_at"])


def _record_agent_task(state: AgentState, record: TaskExecutionRecord) -> None:
    if not state.run:
        return
    sequence = state.run.task_records.count() + 1
    AgentTaskRecord.objects.create(
        run=state.run,
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
            "plan_queue": _serialize_plan_queue(decision.plan_queue),
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


def _serialize_plan_queue(queue: list[AgentPlanStep]) -> list[dict[str, str]]:
    return [{"tool": step.tool, "purpose": step.purpose} for step in queue]


def _truncate_db_text(text: object, limit: int = 12000) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[truncated]"


def _execute_agent_plan(thread: Thread, user_text: str, config, plan: AgentPlan, progress=None, run: AgentRun | None = None):
    state = AgentState.from_plan(plan, user_text, run=run)
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
        rag = _build_rag_input(thread, user_text, preferred_rag_query)
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
        if not can_build_sandbox_program(input_text):
            logger.debug("sandbox_code_generation_start thread_id=%s", thread.id)
            if progress:
                yield _sse(progress("Tool sandbox: generating executable program."))
            _log_tail("llm_prompt", input_text, thread_id=thread.id, purpose="sandbox_code_generation")
            generated_code = generate_sandbox_code(config, input_text)
            if not generated_code.strip():
                plan_trace.append("Sandbox result: skipped before execution; no executable program could be generated.")
                logger.debug(
                    "agent_step_result thread_id=%s tool=sandbox status=no_executable_program",
                    thread.id,
                )
                if progress:
                    yield _sse(progress("Tool sandbox: skipped because no executable program was generated."))
                return {"ok": True, "input_text": _prepend_plan_trace(input_text, plan_trace), "final_message": ""}
            logger.debug(
                "sandbox_code_generation_done thread_id=%s code_preview=%r",
                thread.id,
                generated_code[:240],
            )
            input_text = input_text + "\n\nGenerated sandbox program:\n```python\n" + generated_code + "\n```"
        result = run_sandbox(input_text, config)
        if not result.ok and "Pythonコードまたは計算式を特定できませんでした" in result.output:
            plan_trace.append("Sandbox result: skipped; no executable code or numeric data was found.")
            logger.debug(
                "agent_step_result thread_id=%s tool=sandbox status=skipped_no_code",
                thread.id,
            )
            if progress:
                yield _sse(progress("Tool sandbox: skipped because executable code or numeric data was not found."))
            return {"ok": True, "input_text": _prepend_plan_trace(input_text, plan_trace), "final_message": ""}
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
        artifact_message = _persist_sandbox_artifacts(thread, input_text, result.output)
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


def _persist_sandbox_artifacts(thread: Thread, input_text: str, output: str) -> str:
    requests = _extract_sandbox_artifact_requests(output)
    if not requests:
        requests = _implicit_sandbox_artifact_requests(input_text, output)
    if not requests:
        return ""
    lines = ["Sandbox成果物の保存結果:"]
    for request in requests:
        result = write_allowed_text_file(
            thread.project,
            request["path"],
            request["content"],
            append=request.get("append", False),
        )
        prefix = "OK" if result.ok else "NG"
        lines.append(f"- {prefix}: {result.message}")
    return "\n".join(lines)


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

    requests: list[dict[str, object]] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        artifacts = payload.get("maigent_artifacts", [])
        if not isinstance(artifacts, list):
            continue
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            content = item.get("content")
            if not path or content is None:
                continue
            requests.append(
                {
                    "path": path,
                    "content": str(content),
                    "append": bool(item.get("append", False)),
                }
            )
    return requests[:5]


def _implicit_sandbox_artifact_requests(input_text: str, output: str) -> list[dict[str, object]]:
    if not _looks_like_save_request(input_text):
        return []
    paths = _extract_candidate_paths(input_text)
    if not paths:
        return []
    content = output.strip()
    if not content:
        return []
    return [{"path": paths[-1], "content": content + "\n", "append": False}]


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


def _build_rag_input(thread: Thread, user_text: str, preferred_query: str = "") -> RagResult:
    if not preferred_query.strip() and not _should_search(user_text):
        logger.debug("rag_decision thread_id=%s search=false", thread.id)
        return RagResult(input_text=user_text, searched=False, has_context=False)
    top_k = _get_rag_top_k()
    query = preferred_query.strip() or _build_answer_query(user_text)
    logger.debug("rag_decision thread_id=%s search=true query=%r top_k=%s", thread.id, query, top_k)
    attachments = _collect_allowed_path_context(thread, user_text, top_k=top_k, answer_query=query)
    if not attachments:
        logger.debug("rag_result thread_id=%s status=no_context query=%r", thread.id, query)
        return RagResult(input_text=user_text, searched=True, has_context=False, query=query)
    logger.debug("rag_result thread_id=%s status=has_context query=%r attachments=%s", thread.id, query, len(attachments))
    input_text = (
        user_text
        + f"\n\nRAG search query: {query}"
        + "\n\nRAG context from allowed local files:\n"
        + "\n\n".join(attachments)
        + "\n\nUse the RAG context only when it directly supports the answer. If it does not, say that the allowed files do not contain enough information."
    )
    return RagResult(input_text=input_text, searched=True, has_context=True, query=query)


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
    _log_tail("llm_prompt", prompt, thread_id=thread.id, purpose="rag_candidate_judge")
    try:
        raw = complete_response(config, prompt, instructions).strip()
    except Exception:
        logger.exception("rag_llm_judge_error thread_id=%s", thread.id)
        return []
    _log_tail("rag_candidate_judge_raw", raw, thread_id=thread.id)
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

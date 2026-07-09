import concurrent.futures
import json
import logging
import queue
import re
import time
from dataclasses import dataclass
from pathlib import Path

from django.db import close_old_connections
from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .access import normalize_access_path
from .applications.file_batch import FILE_BATCH_MAX_FILES, FILE_BATCH_SIZE, _build_file_batch_input
from .applications.llm_helpers import (
    FinalEvaluationSettings,
    _evaluate_final_answer,
    _log_tail,
    _prepend_retry_feedback,
    _sse,
    _tool_enabled,
)
from .applications.rag import (
    DEFAULT_RAG_TOP_K,
    DISPLAY_FILE_LIST_LIMIT,
    MAX_RAG_TOP_K,
    _apply_llm_rag_decision,
    _build_answer_query,
    _build_rag_input,
    _format_file_list_for_display,
    _get_rag_top_k,
)
from .applications.planning import (
    INTERNAL_PLAN_TOOL_NAMES,
    KNOWN_TOOL_NAMES,
    AgentState,
    ReplanDecision,
    StoredPlanStep,
    TaskExecutionRecord,
    _allowed_plan_tools,
    _avoid_failed_plan,
    _build_agent_plan_with_llm_tool_selection,
    _filter_plan_to_allowed_tools,
    _format_initial_clarification_message,
    _initial_clarifier_llm_enabled,
    _initial_clarifier_max_output_tokens,
    _initial_clarifier_reasoning_effort,
    _replan_after_step,
    _request_initial_clarification_if_needed,
    _route_final_output,
    _run_precheck_in_parallel,
    _summarize_revised_plan,
    _tool_selector_llm_enabled,
    _tool_selector_max_output_tokens,
    _tool_selector_reasoning_effort,
)
from .applications.multi_agent import (
    _build_multi_agent_workers,
    _consume_generator,
    _create_agent_task_record,
    _create_agent_worker_runs,
    _format_context_worker_results,
    _format_worker_results_for_synthesis,
    _handle_worker_progress,
    _multi_agent_enabled,
    _multi_agent_max_workers,
    _multi_agent_progress_visible,
    _sandbox_context_dependencies,
    _store_worker_result,
    _summarize_worker_plan,
    _truncate_db_text,
    _worker_order,
)
from .applications.artifacts import _persist_final_answer_artifact, _persist_sandbox_artifacts
from .applications.sandbox import (
    _append_sandbox_dataset_manifest,
    _format_sandbox_message,
    _generate_sandbox_code_with_retries,
    _is_sandbox_result_adequate,
    _retry_generated_sandbox_after_execution_failure,
    _sandbox_datasets_from_rag_context,
)
from .applications.web_search import search_web
from .config import RuntimeConfig, load_agents_md, load_runtime_config, load_skills
from .file_broker import allowed_image_mime_type, resolve_project_output_file
from .models import AgentRun, AgentWorkerRun, AppSetting, ApprovalRequest, Automation, FeatureFlag, Message, Project, ProjectAccessPath, Thread
from .openai_client import stream_response
from .prompt_loader import load_prompt
from .slash_commands import handle_slash_command
from .tooling import (
    AgentPlan,
    AgentPlanStep,
    AgentWorkerResult,
    AgentWorkerSpec,
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
DEFAULT_FINAL_EVALUATION_MAX_RETRIES = 3


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
            "skills": load_skills(project.path),
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
                "progress_full": list(progress_lines),
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
    agents_md = load_agents_md(thread.project.path)
    if agents_md:
        lines.append("Project AGENTS.md instructions:")
        lines.append(agents_md)
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
        if not result.get("used_tools", True):
            if progress:
                yield _sse(progress(f"Attempt {attempt}: no tools were used; skipping final evaluation."))
            logger.debug(
                "final_evaluation_skipped thread_id=%s attempt=%s reason=no_tools_used plan=%r",
                thread.id,
                attempt,
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
    if progress:
        yield _sse(progress("Precheck: running clarification check and tool selection in parallel."))
    clarification, plan = _run_precheck_in_parallel(thread, user_text, input_text, config)
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
    if not plan:
        plan = build_agent_plan(user_text, config)
        plan = _apply_llm_rag_decision(thread, user_text, config, plan)
    plan = _avoid_failed_plan(plan, config, failed_plans)
    allowed_tools = _allowed_plan_tools(config, thread)
    plan = _filter_plan_to_allowed_tools(plan, allowed_tools)
    used_tools = any(step.tool != "final" for step in plan.steps)
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
            "used_tools": used_tools,
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
        "used_tools": used_tools,
    }


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

    allowed_tools = _allowed_plan_tools(config, thread)
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
    sandbox_dependencies = _sandbox_context_dependencies(plan, workers)
    if sandbox_dependencies:
        dependency_workers = [worker for worker in workers if worker.name in sandbox_dependencies]
        if dependency_workers:
            dependency_results = yield from _run_worker_round(
                dependency_workers,
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
            results.extend(dependency_results)
            pending_workers = [worker for worker in workers if worker.name not in sandbox_dependencies]
            context_results = sorted(
                [result for result in dependency_results if result.ok and result.input_text],
                key=lambda result: _worker_order(workers, result.name),
            )
            if context_results:
                context_input = context_results[-1].input_text if len(context_results) == 1 else _format_context_worker_results(base_input, context_results)
                for worker in pending_workers:
                    if worker.name == "compute":
                        worker_inputs[worker.name] = context_input

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



def _execute_agent_plan(
    thread: Thread,
    user_text: str,
    config,
    plan: AgentPlan,
    progress=None,
    run: AgentRun | None = None,
    input_text: str | None = None,
):
    allowed_tools = _allowed_plan_tools(config, thread)
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
        has_more_steps = any(queued.tool != "final" for queued in state.plan_queue)
        outcome = yield from _execute_agent_task(
            thread, user_text, config, state.input_text, plan_trace, step, plan.rag_query, progress, has_more_steps=has_more_steps
        )
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
    has_more_steps: bool = False,
):
    logger.debug("agent_step_start thread_id=%s tool=%s purpose=%s", thread.id, step.tool, step.purpose)
    if progress:
        yield _sse(progress(f"Tool {step.tool}: {step.purpose}"))
    if step.tool == "web_search":
        search_query = _build_answer_query(user_text)
        search = search_web(config, search_query)
        if not search.ok:
            logger.debug("agent_step_result thread_id=%s tool=web_search status=failed message=%r", thread.id, search.message)
            if progress:
                yield _sse(progress(f"Tool web_search: {search.message}"))
            return {"ok": False, "input_text": input_text, "final_message": search.message}
        titles_display = ", ".join(item.title for item in search.results[:DISPLAY_FILE_LIST_LIMIT])
        plan_trace.append(f"Web search result: query={search.query}; results={len(search.results)} ({titles_display})")
        logger.debug("agent_step_result thread_id=%s tool=web_search status=adequate query=%r results=%s", thread.id, search.query, len(search.results))
        if progress:
            yield _sse(progress(f"Tool web_search: found {len(search.results)} results for '{search.query}'; {titles_display}."))
        search_context = "\n\n".join(
            f"- {item.title}\n  {item.url}\n  {item.snippet}" if item.snippet else f"- {item.title}\n  {item.url}"
            for item in search.results
        )
        web_input_text = (
            input_text
            + f"\n\nWeb search query: {search.query}"
            + "\n\nWeb search results:\n"
            + search_context
            + "\n\nUse the web search results only when they directly support the answer."
        )
        return {"ok": True, "input_text": _prepend_plan_trace(web_input_text, plan_trace), "final_message": ""}
    if step.tool == "rag":
        rag = _build_rag_input(thread, user_text, preferred_rag_query, input_text)
        if rag.searched and not rag.has_context:
            logger.debug(
                "agent_step_result thread_id=%s tool=rag status=no_context query=%r has_more_steps=%s",
                thread.id,
                rag.query,
                has_more_steps,
            )
            if progress:
                yield _sse(progress("Tool rag: no adequate context found."))
            if has_more_steps:
                plan_trace.append("RAG result: no adequate local context found; continuing with the remaining plan.")
                return {"ok": True, "input_text": _prepend_plan_trace(input_text, plan_trace), "final_message": ""}
            return {
                "ok": True,
                "input_text": input_text,
                "final_message": "許可済みファイル内に、この質問へ回答するための十分な情報が見つかりませんでした。",
            }
        files_display = _format_file_list_for_display(rag.paths)
        plan_trace.append(f"RAG result: adequate; query={rag.query or '(none)'}; files={files_display or '(none)'}")
        logger.debug("agent_step_result thread_id=%s tool=rag status=adequate query=%r files=%s", thread.id, rag.query, rag.paths)
        if progress:
            yield _sse(progress(f"Tool rag: context found for query '{rag.query or '(none)'}'; files: {files_display or '(none)'}."))
        return {"ok": True, "input_text": _prepend_plan_trace(rag.input_text, plan_trace), "final_message": ""}
    if step.tool == "file_batch":
        result = yield from _build_file_batch_input(thread, user_text, config, input_text, progress=progress)
        if not result.ok:
            logger.debug("agent_step_result thread_id=%s tool=file_batch status=failed message=%r", thread.id, result.final_message[:240])
            if progress:
                yield _sse(progress("Tool file_batch: no readable target files found."))
            return {"ok": False, "input_text": input_text, "final_message": result.final_message}
        files_display = _format_file_list_for_display(result.paths)
        plan_trace.append(f"File batch result: map-reduce context prepared; files={len(result.paths)} ({files_display or '(none)'})")
        logger.debug("agent_step_result thread_id=%s tool=file_batch status=adequate files=%s", thread.id, result.paths)
        if progress:
            yield _sse(progress(f"Tool file_batch: map-reduce context prepared for {len(result.paths)} files; files: {files_display or '(none)'}."))
        return {"ok": True, "input_text": _prepend_plan_trace(result.input_text, plan_trace), "final_message": ""}
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
    if step.tool.startswith("skill:"):
        skill_name = step.tool[len("skill:") :]
        skill = next((item for item in load_skills(thread.project.path) if item.name == skill_name), None)
        if not skill:
            logger.debug("agent_step_result thread_id=%s tool=%s status=not_found", thread.id, step.tool)
            if progress:
                yield _sse(progress(f"Tool {step.tool}: skill definition not found."))
            return {"ok": False, "input_text": input_text, "final_message": f"スキル '{skill_name}' が見つかりませんでした。"}
        plan_trace.append(f"Skill result: applied '{skill.name}' instructions from {skill.path}.")
        logger.debug("agent_step_result thread_id=%s tool=%s status=adequate path=%s", thread.id, step.tool, skill.path)
        if progress:
            yield _sse(progress(f"Tool {step.tool}: applying skill instructions."))
        skill_input_text = input_text + f"\n\nSkill instructions to follow ({skill.name}):\n{skill.body}"
        return {"ok": True, "input_text": _prepend_plan_trace(skill_input_text, plan_trace), "final_message": ""}
    return {"ok": True, "input_text": input_text, "final_message": ""}


def _prepend_plan_trace(input_text: str, plan_trace: list[str]) -> str:
    return "\n".join(plan_trace) + "\n\n" + input_text


def _tool_settings(config) -> list[dict[str, str]]:
    sandbox_libraries = ", ".join(config.sandbox_allowed_libraries) or "none"
    return [
        {"name": "rag", "enabled": "on" if config.tool_enabled("rag", default=True) else "off", "detail": "BM25 local files"},
        {
            "name": "file_batch",
            "enabled": "on" if config.tool_enabled("file_batch") else "off",
            "detail": f"map-reduce local files; batch size: {FILE_BATCH_SIZE}; max files: {FILE_BATCH_MAX_FILES}",
        },
        {
            "name": "web_search",
            "enabled": "on" if config.tool_enabled("web_search") else "off",
            "detail": "Tavily API" + ("" if config.web_search_api_key else "; api_key not configured"),
        },
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



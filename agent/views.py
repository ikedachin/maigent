import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .access import is_path_allowed
from .access import normalize_access_path
from .config import load_runtime_config
from .models import AppSetting, ApprovalRequest, Automation, FeatureFlag, Message, Project, ProjectAccessPath, Thread
from .openai_client import generate_sandbox_code, stream_response
from .slash_commands import handle_slash_command
from .tooling import AgentPlan, build_agent_plan, can_build_sandbox_program, run_sandbox

logger = logging.getLogger("agent")

MAX_CONTEXT_FILE_CHARS = 8000
AUTO_CONTEXT_FILE_CHARS = 3000
AUTO_CONTEXT_MAX_FILES = 3
DEFAULT_RAG_TOP_K = 3
MAX_RAG_TOP_K = 10
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


def dashboard(request, thread_id=None):
    project, thread = _ensure_defaults()
    if thread_id:
        thread = get_object_or_404(Thread, id=thread_id)
        project = thread.project
    config = load_runtime_config(project.path)
    rag_top_k = _get_rag_top_k()
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
        content_parts: list[str] = []
        response_id = ""
        assistant.status = "streaming"
        assistant.save(update_fields=["status"])
        try:
            instructions = _build_instructions(thread)
            plan = build_agent_plan(latest_user.content, config)
            logger.debug(
                "agent_plan thread_id=%s message_id=%s summary=%s steps=%s",
                thread.id,
                latest_user.id,
                plan.summary,
                [f"{step.tool}:{step.purpose}" for step in plan.steps],
            )
            plan_result = _execute_agent_plan(thread, latest_user.content, config, plan)
            if plan_result["final_message"]:
                message = plan_result["final_message"]
                assistant.content = message
                assistant.status = "complete" if plan_result["ok"] else "error"
                assistant.save(update_fields=["content", "status"])
                yield f"data: {json.dumps({'delta': message})}\n\n"
                yield f"data: {json.dumps({'done': True, 'message_id': assistant.id})}\n\n"
                return
            input_text = plan_result["input_text"]
            logger.debug("llm_start thread_id=%s input_chars=%s", thread.id, len(input_text))
            for kind, payload in stream_response(config, input_text, instructions):
                if kind == "delta":
                    content_parts.append(payload)
                    yield f"data: {json.dumps({'delta': payload})}\n\n"
                elif kind == "response_id":
                    response_id = payload
            assistant.content = "".join(content_parts)
            assistant.status = "complete"
            assistant.openai_response_id = response_id
            assistant.save(update_fields=["content", "status", "openai_response_id"])
            logger.debug(
                "llm_done thread_id=%s assistant_id=%s output_chars=%s response_id=%s",
                thread.id,
                assistant.id,
                len(assistant.content),
                response_id or "",
            )
            yield f"data: {json.dumps({'done': True, 'message_id': assistant.id})}\n\n"
        except Exception as exc:
            assistant.content = str(exc)
            assistant.status = "error"
            assistant.save(update_fields=["content", "status"])
            logger.exception("agent_error thread_id=%s assistant_id=%s", thread.id, assistant.id)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    response = StreamingHttpResponse(events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    return response


def _build_instructions(thread: Thread) -> str:
    lines = [
        "You are a pragmatic local coding agent inside a Django web app.",
        "Do not claim to have executed shell commands. If execution is needed, propose an approval request.",
    ]
    if thread.memory_enabled and thread.summary:
        lines.append("Thread memory summary:")
        lines.append(thread.summary)
    return "\n".join(lines)


def _format_sandbox_message(ok: bool, output: str) -> str:
    status = "成功" if ok else "失敗"
    return f"Sandbox実行結果: {status}\n\n```text\n{output}\n```"


def _execute_agent_plan(thread: Thread, user_text: str, config, plan: AgentPlan) -> dict[str, object]:
    input_text = user_text
    plan_trace = [f"Agent plan: {plan.summary}"]
    for step in plan.steps:
        logger.debug("agent_step_start thread_id=%s tool=%s purpose=%s", thread.id, step.tool, step.purpose)
        if step.tool == "final":
            continue
        if step.tool == "web_search":
            logger.debug("agent_step_result thread_id=%s tool=web_search status=unimplemented", thread.id)
            return {
                "ok": True,
                "input_text": input_text,
                "final_message": "Web検索ツールは計画で選択されましたが、まだ未実装です。",
            }
        if step.tool == "rag":
            rag = _build_rag_input(thread, user_text)
            if rag.searched and not rag.has_context:
                logger.debug("agent_step_result thread_id=%s tool=rag status=no_context query=%r", thread.id, rag.query)
                return {
                    "ok": True,
                    "input_text": input_text,
                    "final_message": "許可済みファイル内に、この質問へ回答するための十分な情報が見つかりませんでした。",
                }
            plan_trace.append(f"RAG result: adequate; query={rag.query or '(none)'}")
            logger.debug("agent_step_result thread_id=%s tool=rag status=adequate query=%r", thread.id, rag.query)
            input_text = _prepend_plan_trace(rag.input_text, plan_trace)
            continue
        if step.tool == "sandbox":
            if not can_build_sandbox_program(input_text):
                logger.debug("sandbox_code_generation_start thread_id=%s", thread.id)
                generated_code = generate_sandbox_code(config, input_text)
                if not generated_code.strip():
                    plan_trace.append("Sandbox result: skipped before execution; no executable program could be generated.")
                    logger.debug(
                        "agent_step_result thread_id=%s tool=sandbox status=no_executable_program",
                        thread.id,
                    )
                    input_text = _prepend_plan_trace(input_text, plan_trace)
                    continue
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
                input_text = _prepend_plan_trace(input_text, plan_trace)
                continue
            if not _is_sandbox_result_adequate(result.ok, result.output):
                logger.debug(
                    "agent_step_result thread_id=%s tool=sandbox status=failed output_preview=%r",
                    thread.id,
                    result.output[:240],
                )
                return {"ok": False, "input_text": input_text, "final_message": _format_sandbox_message(result.ok, result.output)}
            plan_trace.append("Sandbox result: adequate")
            logger.debug(
                "agent_step_result thread_id=%s tool=sandbox status=adequate output_preview=%r",
                thread.id,
                result.output[:240],
            )
            return {"ok": True, "input_text": input_text, "final_message": _format_sandbox_message(True, result.output)}
    if plan_trace and plan.steps[0].tool != "final":
        input_text = _prepend_plan_trace(input_text, plan_trace)
    return {"ok": True, "input_text": input_text, "final_message": ""}


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


def _build_rag_input(thread: Thread, user_text: str) -> RagResult:
    if not _should_search(user_text):
        logger.debug("rag_decision thread_id=%s search=false", thread.id)
        return RagResult(input_text=user_text, searched=False, has_context=False)
    top_k = _get_rag_top_k()
    query = _build_answer_query(user_text)
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
        cleaned = match.rstrip("。、,.):;]")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            paths.append(cleaned)
    return paths


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
        return []
    logger.debug("bm25_adequacy thread_id=%s adequate=true", thread.id)
    relevant = [(score, path) for score, path in ranked if score >= RAG_MIN_BM25_SCORE]
    return [_read_context_file_limited(path, AUTO_CONTEXT_FILE_CHARS) for score, path in relevant[:top_k]]


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
    tokenized = [(_tokenize_for_bm25(text), path) for path, text in documents]
    avgdl = sum(len(tokens) for tokens, _ in tokenized) / len(tokenized)
    doc_freq: dict[str, int] = {}
    for tokens, _ in tokenized:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    k1 = 1.5
    b = 0.75
    ranked: list[tuple[float, Path]] = []
    for tokens, path in tokenized:
        if not tokens:
            continue
        score = 0.0
        length = len(tokens)
        for term in query_terms:
            tf = tokens.count(term)
            if tf == 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log((len(documents) - df + 0.5) / (df + 0.5) + 1)
            denom = tf + k1 * (1 - b + b * length / avgdl)
            score += idf * (tf * (k1 + 1)) / denom
        if score > 0:
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

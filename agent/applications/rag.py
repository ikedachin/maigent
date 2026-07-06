import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..access import is_path_allowed
from ..config import load_runtime_config
from ..models import AppSetting, Project, Thread
from ..prompt_loader import load_prompt
from ..tooling import AgentPlan, AgentPlanStep
from .llm_helpers import (
    _complete_response_with_retries,
    _extract_labeled_value,
    _log_tail,
    _parse_integer_list_from_text,
    _text_value,
    _tool_enabled,
)

logger = logging.getLogger("agent")

MAX_CONTEXT_FILE_CHARS = 8000
AUTO_CONTEXT_FILE_CHARS = 3000
AUTO_CONTEXT_MAX_FILES = 3
DEFAULT_RAG_TOP_K = 3
MAX_RAG_TOP_K = 10
RAG_MIN_BM25_SCORE = 0.1
DISPLAY_FILE_LIST_LIMIT = 5
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
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class LlmRagDecision:
    should_search: bool
    query: str
    reason: str


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
    return RagResult(input_text=input_text, searched=True, has_context=True, query=query, paths=_extract_attachment_paths(attachments))


def _extract_attachment_paths(attachments: list[str]) -> tuple[str, ...]:
    paths: list[str] = []
    for attachment in attachments:
        first_line = attachment.splitlines()[0] if attachment else ""
        match = re.match(r"^(?:File|Folder|Auto-selected file): (.+)$", first_line)
        if match:
            paths.append(match.group(1).strip())
    return tuple(paths)


def _format_file_list_for_display(paths: tuple[str, ...], limit: int = DISPLAY_FILE_LIST_LIMIT) -> str:
    if not paths:
        return ""
    names = [Path(path).name for path in paths[:limit]]
    remainder = len(paths) - len(names)
    display = ", ".join(names)
    if remainder > 0:
        display += f", +{remainder} more"
    return display


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
    if any(term in top_path.name.lower() for term in query_terms):
        return True
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
        if _is_project_output_path(thread.project, root):
            continue
        candidates = [root] if root.is_file() else _iter_context_files(root)
        for path in candidates:
            if path in seen or not is_path_allowed(thread.project, str(path), write=False):
                continue
            if _is_project_output_path(thread.project, path):
                continue
            seen.add(path)
            try:
                sample = path.read_text(encoding="utf-8")[:12000]
            except (UnicodeDecodeError, OSError):
                continue
            documents.append((path, f"{path.name}\n{sample}"))
    return documents


def _is_project_output_path(project: Project, path: Path) -> bool:
    output_path = (project.output_path or "").strip()
    if not output_path:
        return False
    try:
        output_root = Path(output_path).expanduser().resolve()
    except OSError:
        return False
    return path == output_root or output_root in path.parents


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
        "入力",
        "フォルダ",
        "フォルダ内",
        "ファイル",
        "いくつか",
        "あります",
        "それ",
        "一つ",
        "一つの",
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
        expanded = _expand_search_term(term)
        if re.search(r"[_.-]", term):
            expanded.extend(part for part in re.split(r"[_.-]+", term) if len(part) >= 3)
        for item in expanded:
            if item not in stopwords and item not in terms:
                terms.append(item)
    return terms[:12]


def _expand_search_term(term: str) -> list[str]:
    if not re.search(r"[一-龥ぁ-んァ-ン]", term):
        return [term]
    normalized = re.sub(
        r"(ください|お願いします|しています|あります|しました|します|して|した|する|できる|ください)$",
        "",
        term,
    )
    parts = []
    for part in re.split(r"(?:について|して|した|する|の|を|に|へ|で|と|が|は|も|から|まで|内|中)", normalized):
        cleaned = re.sub(r"(ください|お願いします|しています|あります|しました|します|して|した|する|できる)$", "", part)
        if len(cleaned) >= 2:
            parts.append(cleaned)
    return parts or [term]


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


def _get_rag_top_k() -> int:
    setting = AppSetting.objects.filter(key="rag_top_k").first()
    if not setting:
        return DEFAULT_RAG_TOP_K
    try:
        value = int(setting.value)
    except ValueError:
        return DEFAULT_RAG_TOP_K
    return max(1, min(MAX_RAG_TOP_K, value))


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

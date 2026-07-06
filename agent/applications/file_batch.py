import concurrent.futures
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..access import is_path_allowed
from ..models import Thread
from ..prompt_loader import load_prompt
from .llm_helpers import (
    _complete_response_with_retries,
    _control_config_int,
    _control_config_max_output_tokens,
    _control_config_reasoning_effort,
    _sse,
)
from .multi_agent import _multi_agent_enabled, _multi_agent_max_workers, _multi_agent_parallel_tools
from .rag import _extract_candidate_paths, _iter_context_files

logger = logging.getLogger("agent")

FILE_BATCH_MAX_FILES = 60
FILE_BATCH_CHARS_PER_FILE = 4000
FILE_BATCH_SIZE = 5
FILE_BATCH_MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class FileBatchItem:
    path: Path
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class FileBatchResult:
    input_text: str
    ok: bool
    final_message: str = ""
    paths: tuple[str, ...] = ()


def _build_file_batch_input(thread: Thread, user_text: str, config, answer_text: str, progress=None):
    items, omitted = _collect_file_batch_items(thread, user_text)
    if not items:
        return FileBatchResult(
            input_text=answer_text,
            ok=False,
            final_message="許可済みフォルダ内に、バッチ処理できるUTF-8テキストファイルが見つかりませんでした。",
        )
    batches = [items[index : index + FILE_BATCH_SIZE] for index in range(0, len(items), FILE_BATCH_SIZE)]
    worker_count = _file_batch_worker_count(config, len(batches))
    logger.debug(
        "file_batch_start thread_id=%s files=%s batches=%s workers=%s omitted=%s",
        thread.id,
        len(items),
        len(batches),
        worker_count,
        omitted,
    )
    if progress:
        yield _sse(progress(f"Tool file_batch: scanning complete; {len(items)} files in {len(batches)} batches."))

    results: list[dict[str, object]] = []
    if worker_count <= 1 or len(batches) <= 1:
        for index, batch in enumerate(batches, start=1):
            if progress:
                yield _sse(progress(f"Tool file_batch: mapping batch {index}/{len(batches)}."))
            results.extend(_map_file_batch(config, user_text, batch, index))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_index = {
                executor.submit(_map_file_batch, config, user_text, batch, index): index for index, batch in enumerate(batches, start=1)
            }
            pending = set(future_to_index)
            while pending:
                done, pending = concurrent.futures.wait(pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    index = future_to_index[future]
                    try:
                        batch_results = future.result()
                    except Exception as exc:
                        logger.exception("file_batch_map_error thread_id=%s batch=%s", thread.id, index)
                        batch_results = [
                            {
                                "path": str(item.path),
                                "summary": f"処理中にエラーが発生しました: {exc}",
                                "status": "error",
                            }
                            for item in batches[index - 1]
                        ]
                    results.extend(batch_results)
                    if progress:
                        completed = len(future_to_index) - len(pending)
                        yield _sse(progress(f"Tool file_batch: completed batch {index}/{len(batches)} ({completed}/{len(batches)} done)."))

    order = {str(item.path): position for position, item in enumerate(items)}
    results.sort(key=lambda item: order.get(str(item.get("path") or ""), len(order)))
    context = _format_file_batch_context(user_text, items, results, omitted, worker_count, len(batches))
    paths = tuple(str(item.path) for item in items)
    return FileBatchResult(input_text=f"{answer_text}\n\n{context}", ok=True, paths=paths)


def _file_batch_worker_count(config, batch_count: int) -> int:
    if batch_count <= 1:
        return 1
    if not _multi_agent_enabled(config) or not _multi_agent_parallel_tools(config):
        return 1
    return max(1, min(batch_count, _multi_agent_max_workers(config)))


def _collect_file_batch_items(thread: Thread, user_text: str) -> tuple[list[FileBatchItem], int]:
    paths = _resolve_file_batch_paths(thread, user_text)
    items: list[FileBatchItem] = []
    omitted = 0
    for path in paths:
        if len(items) >= FILE_BATCH_MAX_FILES:
            omitted += 1
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            omitted += 1
            continue
        truncated = len(content) > FILE_BATCH_CHARS_PER_FILE
        if truncated:
            content = content[:FILE_BATCH_CHARS_PER_FILE] + "\n[truncated]"
        items.append(FileBatchItem(path=path, text=content, truncated=truncated))
    return items, omitted


def _resolve_file_batch_paths(thread: Thread, user_text: str) -> list[Path]:
    roots: list[Path] = []
    for path_text in _extract_candidate_paths(user_text):
        try:
            path = Path(path_text).expanduser().resolve()
        except OSError:
            continue
        if is_path_allowed(thread.project, str(path), write=False) and path.exists():
            roots.append(path)
    if not roots:
        for access in thread.project.access_paths.filter(mode__in=["read", "write"]):
            try:
                path = Path(access.path).expanduser().resolve()
            except OSError:
                continue
            if is_path_allowed(thread.project, str(path), write=False) and path.exists():
                roots.append(path)

    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidates = [root] if root.is_file() else _iter_context_files(root)
        for path in candidates:
            if path in seen or not is_path_allowed(thread.project, str(path), write=False):
                continue
            seen.add(path)
            files.append(path)
    return sorted(files, key=lambda item: str(item).lower())


def _map_file_batch(config, user_text: str, batch: list[FileBatchItem], batch_index: int) -> list[dict[str, object]]:
    instructions = load_prompt("file_batch_map_instructions.txt")
    file_blocks = []
    for item in batch:
        file_blocks.append(
            "File path: "
            + str(item.path)
            + ("\nTruncated: yes" if item.truncated else "\nTruncated: no")
            + "\nContent:\n```text\n"
            + item.text
            + "\n```"
        )
    prompt = load_prompt(
        "file_batch_map_prompt.txt",
        user_text=user_text,
        batch_index=batch_index,
        file_blocks="\n\n".join(file_blocks),
    )
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="file_batch_map",
        config_name="file_batch",
        max_output_tokens=_file_batch_max_output_tokens(config),
        reasoning_effort=_file_batch_reasoning_effort(config),
        temperature=0,
        max_retries=_file_batch_max_retries(config),
        log_exceptions=False,
    )
    parsed = _parse_file_batch_map_response(raw, batch)
    if parsed:
        return parsed
    return [
        {
            "path": str(item.path),
            "summary": _heuristic_file_summary(item),
            "status": "fallback",
            "truncated": item.truncated,
        }
        for item in batch
    ]


def _parse_file_batch_map_response(raw: str, batch: list[FileBatchItem]) -> list[dict[str, object]]:
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            payload = json.loads(_extract_json_array(raw))
        except Exception:
            return []
    if not isinstance(payload, list):
        return []
    allowed = {str(item.path) for item in batch}
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if path not in allowed or path in seen:
            continue
        summary = " ".join(str(item.get("summary") or "").split())
        if not summary:
            continue
        status = str(item.get("status") or "ok").strip() or "ok"
        seen.add(path)
        results.append({"path": path, "summary": summary[:500], "status": status})
    missing = [item for item in batch if str(item.path) not in seen]
    for item in missing:
        results.append({"path": str(item.path), "summary": _heuristic_file_summary(item), "status": "fallback", "truncated": item.truncated})
    return results


def _extract_json_array(text: str) -> str:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        array_start = text.find("[", index)
        if array_start < 0:
            raise ValueError("JSON array not found")
        try:
            payload, length = decoder.raw_decode(text[array_start:])
        except json.JSONDecodeError:
            index = array_start + 1
            continue
        if isinstance(payload, list):
            return text[array_start : array_start + length]
        index = array_start + max(1, length)
    raise ValueError("JSON array not found")


def _heuristic_file_summary(item: FileBatchItem) -> str:
    first_line = next((line.strip() for line in item.text.splitlines() if line.strip()), "")
    if first_line:
        return first_line[:160]
    return "空または内容を要約できないテキストファイルです。"


def _format_file_batch_context(
    user_text: str,
    items: list[FileBatchItem],
    results: list[dict[str, object]],
    omitted: int,
    worker_count: int,
    batch_count: int,
) -> str:
    payload = {
        "request": user_text,
        "files_considered": len(items),
        "omitted_files": omitted,
        "map_batches": batch_count,
        "map_workers": worker_count,
        "results": results,
    }
    lines = [
        "File batch map-reduce context:",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "",
        "Use the file batch context as map results. Reduce them into the final answer requested by the user.",
        "If omitted_files is greater than 0, clearly mention that some files were skipped because of limits or read errors.",
    ]
    return "\n".join(lines)


def _file_batch_max_output_tokens(config) -> int:
    return _control_config_max_output_tokens(config, "file_batch", FILE_BATCH_MAX_OUTPUT_TOKENS)


def _file_batch_reasoning_effort(config) -> str:
    return _control_config_reasoning_effort(config, "file_batch", "reasoning_effort", "none")


def _file_batch_max_retries(config) -> int:
    return _control_config_int(config, "file_batch", "max_retries", 0, minimum=0, maximum=5)

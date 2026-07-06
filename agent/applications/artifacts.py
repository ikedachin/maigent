import json
import re
from pathlib import Path

from django.urls import reverse

from ..file_broker import allowed_image_mime_type, write_allowed_binary_file, write_allowed_text_file
from ..models import Project, Thread
from .llm_helpers import _extract_json_object
from .rag import _extract_candidate_paths


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

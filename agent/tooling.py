import ast
import csv
import json
import logging
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import RuntimeConfig
from .prompt_loader import load_prompt

logger = logging.getLogger("agent")


@dataclass(frozen=True)
class ToolDecision:
    name: str
    reason: str


@dataclass(frozen=True)
class AgentPlanStep:
    tool: str
    purpose: str


@dataclass(frozen=True)
class AgentPlan:
    goal: str
    evaluation_criteria: list[str]
    summary: str
    steps: list[AgentPlanStep]
    rag_query: str = ""


@dataclass(frozen=True)
class AgentWorkerSpec:
    name: str
    role: str
    purpose: str
    steps: tuple[AgentPlanStep, ...]


@dataclass(frozen=True)
class AgentWorkerResult:
    name: str
    role: str
    purpose: str
    ok: bool
    input_text: str
    result: str
    error: str = ""
    task_records: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    output: str
    artifacts: tuple[dict[str, object], ...] = ()
    raw_output: str = ""


@dataclass(frozen=True)
class SandboxDataset:
    id: str
    name: str
    path: str
    kind: str
    text: str
    columns: tuple[str, ...] = ()
    row_count: int = 0
    truncated: bool = False


def build_sandbox_program(message: str) -> str:
    if requires_llm_sandbox_program(message):
        return ""
    return (
        _extract_python_code(message)
        or _python_for_tabular_task(message)
        or _python_for_expression(message)
        or _python_for_numeric_task(message)
    )


def can_build_sandbox_program(message: str) -> bool:
    return bool(build_sandbox_program(message))


def requires_llm_sandbox_program(message: str) -> bool:
    question = _strip_rag_context(message)
    lowered = question.lower()
    group_markers = ["ごと", "ごとの", "別", "別に", "別の", "group by", "grouped by", "by name", "by subject"]
    if any(marker in question or marker in lowered for marker in group_markers):
        return True
    visual_markers = [
        "画像",
        "ヒストグラム",
        "度数分布",
        "グラフ",
        "プロット",
        "image",
        "histogram",
        "chart",
        "graph",
        "plot",
    ]
    if any(marker in question or marker in lowered for marker in visual_markers):
        return True
    return False


def build_agent_plan(message: str, config: RuntimeConfig) -> AgentPlan:
    goal = build_agent_goal(message)
    evaluation_criteria = build_agent_evaluation_criteria(message)
    steps: list[AgentPlanStep] = []
    if _config_tool_enabled(config, "web_search") and _looks_like_web_search(message):
        steps.append(AgentPlanStep("web_search", "Collect current or external information."))
    if _config_tool_enabled(config, "rag", default=True) and _looks_like_rag_task(message):
        steps.append(AgentPlanStep("rag", "Search allowed local files with BM25 and attach relevant context."))
    if _config_tool_enabled(config, "sandbox") and _looks_like_sandbox_task(message):
        steps.append(AgentPlanStep("sandbox", "Run deterministic Python in Docker for exact computation."))
    if not steps:
        steps.append(AgentPlanStep("final", "Answer directly with the language model."))
    plan = AgentPlan(goal=goal, evaluation_criteria=evaluation_criteria, summary=_summarize_plan(steps), steps=steps)
    logger.debug(
        "tool_plan_built goal=%r criteria=%s summary=%s steps=%s",
        plan.goal,
        plan.evaluation_criteria,
        plan.summary,
        [step.tool for step in plan.steps],
    )
    return plan


def build_agent_worker_specs(plan: AgentPlan, max_workers: int = 3, parallel_tools: bool = True) -> list[AgentWorkerSpec]:
    max_workers = max(1, min(5, int(max_workers or 1)))
    tool_steps = [step for step in plan.steps if step.tool != "final"]
    if not tool_steps or not parallel_tools:
        return []

    specs: list[AgentWorkerSpec] = []
    research_steps = tuple(step for step in tool_steps if step.tool in {"rag", "web_search"})
    compute_steps = tuple(step for step in tool_steps if step.tool == "sandbox")
    other_steps = tuple(step for step in tool_steps if step.tool not in {"rag", "web_search", "sandbox"})

    if research_steps:
        specs.append(
            AgentWorkerSpec(
                name="research",
                role="research",
                purpose="Collect local or external context for the request.",
                steps=research_steps,
            )
        )
    if compute_steps:
        specs.append(
            AgentWorkerSpec(
                name="compute",
                role="compute",
                purpose="Run deterministic computation or artifact generation.",
                steps=compute_steps,
            )
        )
    if other_steps:
        specs.append(
            AgentWorkerSpec(
                name="analysis",
                role="analysis",
                purpose="Run remaining analysis tasks.",
                steps=other_steps,
            )
        )
    if len(specs) >= 2 and len(specs) < max_workers:
        specs.append(
            AgentWorkerSpec(
                name="verify",
                role="verify",
                purpose="Review worker outputs during synthesis for gaps or contradictions.",
                steps=(),
            )
        )
    return specs[:max_workers]


def build_agent_goal(message: str) -> str:
    text = " ".join(message.strip().split())
    if not text:
        return "Answer the user's request."
    if len(text) > 180:
        text = text[:177].rstrip() + "..."
    return f"Answer the user's request: {text}"


def build_agent_evaluation_criteria(message: str) -> list[str]:
    criteria = evaluation_criteria_section("base")
    lowered = message.lower()
    if _looks_like_rag_task(message):
        criteria.append(evaluation_criterion("rag"))
    if _looks_like_sandbox_task(message):
        criteria.append(evaluation_criterion("sandbox"))
    if any(marker in lowered or marker in message for marker in ["要約", "summary", "summarize"]):
        criteria.append(evaluation_criterion("summary"))
    if any(marker in lowered or marker in message for marker in ["一覧", "list", "列挙"]):
        criteria.append(evaluation_criterion("list"))
    return _dedupe_criteria(criteria)


def evaluation_criterion(name: str) -> str:
    return " ".join(evaluation_criteria_section(name))


def evaluation_criteria_section(name: str) -> list[str]:
    sections = _load_evaluation_criteria_sections()
    return list(sections.get(name, []))


def _load_evaluation_criteria_sections() -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in load_prompt("evaluation_criteria.txt").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = re.fullmatch(r"\[([a-zA-Z0-9_-]+)\]", line)
        if section_match:
            current = section_match.group(1)
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(line.removeprefix("-").strip())
    return sections


def _dedupe_criteria(criteria: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for criterion in criteria:
        value = criterion.strip()
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def select_tool(message: str, config: RuntimeConfig) -> ToolDecision:
    if _config_tool_enabled(config, "sandbox") and _looks_like_sandbox_task(message):
        decision = ToolDecision("sandbox", "calculation_or_code_execution")
        logger.debug("tool_selected name=%s reason=%s", decision.name, decision.reason)
        return decision
    if _config_tool_enabled(config, "web_search") and _looks_like_web_search(message):
        decision = ToolDecision("web_search", "current_or_external_information")
        logger.debug("tool_selected name=%s reason=%s", decision.name, decision.reason)
        return decision
    if _config_tool_enabled(config, "rag", default=True) and _looks_like_rag_task(message):
        decision = ToolDecision("rag", "local_file_context")
        logger.debug("tool_selected name=%s reason=%s", decision.name, decision.reason)
        return decision
    decision = ToolDecision("none", "direct_generation")
    logger.debug("tool_selected name=%s reason=%s", decision.name, decision.reason)
    return decision


def _summarize_plan(steps: list[AgentPlanStep]) -> str:
    names = [step.tool for step in steps]
    if names == ["final"]:
        return "Direct answer; no tool use needed."
    return " -> ".join(names) + " -> final"


def _config_tool_enabled(config: RuntimeConfig, name: str, default: bool = False) -> bool:
    try:
        value = config.tool_enabled(name, default=default)
    except AttributeError:
        return default
    return value if isinstance(value, bool) else default


def run_sandbox(message: str, config: RuntimeConfig, code: str = "", datasets: tuple[SandboxDataset, ...] = ()) -> SandboxResult:
    code = code.strip() if code else build_sandbox_program(message)
    if not code:
        logger.debug("sandbox_skip reason=no_code")
        return SandboxResult(False, "sandboxで実行できるPythonコードまたは計算式を特定できませんでした。")
    code = _prepend_sandbox_datasets(code, datasets)
    code = _prepend_sandbox_runtime_prelude(code)
    with tempfile.TemporaryDirectory() as tmp_dir:
        script = Path(tmp_dir) / "script.py"
        script.write_text(code, encoding="utf-8")
        install = ""
        libraries = config.sandbox_allowed_libraries
        install_on_run = getattr(config, "sandbox_install_libraries_on_run", False)
        if libraries and install_on_run:
            install = "pip install --no-cache-dir " + " ".join(shlex.quote(lib) for lib in libraries) + " && "
        network = [] if install else ["--network", "none"]
        command = [
            "docker",
            "run",
            "--rm",
            *network,
            "-v",
            f"{tmp_dir}:/work:ro",
            "-w",
            "/work",
            config.sandbox_image,
            "sh",
            "-lc",
            install + "python /work/script.py",
        ]
        safe_command = [part if part != f"{tmp_dir}:/work:ro" else "<tmp>:/work:ro" for part in command]
        logger.debug(
            "sandbox_start image=%s timeout=%s libraries=%s command=%s code_preview=%r",
            config.sandbox_image,
            config.sandbox_timeout_seconds,
            libraries if install_on_run else "preinstalled",
            safe_command,
            code[:240],
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=config.sandbox_timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            logger.debug("sandbox_error reason=docker_not_found")
            return SandboxResult(False, "Dockerが見つかりません。Dockerをインストールして起動してください。")
        except subprocess.TimeoutExpired:
            logger.debug("sandbox_error reason=timeout")
            return SandboxResult(False, "sandbox実行がタイムアウトしました。")
    raw_output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    output, artifacts = _parse_typed_sandbox_output(raw_output)
    output = output.strip() or "(no output)"
    logger.debug("sandbox_done returncode=%s output_preview=%r artifacts=%s", completed.returncode, output[:240], len(artifacts))
    return SandboxResult(completed.returncode == 0, output[:12000], tuple(artifacts[:5]), raw_output[:12000])


def make_sandbox_dataset(
    dataset_id: str,
    name: str,
    path: str,
    kind: str,
    text: str,
    truncated: bool = False,
) -> SandboxDataset:
    columns, row_count = _tabular_dataset_shape(kind, text)
    return SandboxDataset(
        id=dataset_id,
        name=name,
        path=path,
        kind=kind,
        text=text,
        columns=tuple(columns),
        row_count=row_count,
        truncated=truncated,
    )


def sandbox_dataset_manifest(datasets: tuple[SandboxDataset, ...]) -> str:
    if not datasets:
        return ""
    lines = [
        "Sandbox datasets are available through the host-provided API below.",
        "Do not copy or rewrite dataset contents into generated code.",
        "Use load_dataset(dataset_id) for tabular data and dataset_text(dataset_id) only when raw text is required.",
        "Available datasets:",
    ]
    for dataset in datasets:
        columns = ", ".join(dataset.columns) if dataset.columns else "(unknown)"
        row_count = str(dataset.row_count) if dataset.row_count else "(unknown)"
        truncated = " yes" if dataset.truncated else " no"
        lines.append(
            f"- id: {dataset.id}; name: {dataset.name}; kind: {dataset.kind}; rows: {row_count}; "
            f"columns: {columns}; truncated:{truncated}"
        )
    return "\n".join(lines)


def _prepend_sandbox_datasets(code: str, datasets: tuple[SandboxDataset, ...]) -> str:
    if not datasets:
        return code
    payload = [
        {
            "id": dataset.id,
            "name": dataset.name,
            "path": dataset.path,
            "kind": dataset.kind,
            "text": dataset.text,
            "columns": list(dataset.columns),
            "row_count": dataset.row_count,
            "truncated": dataset.truncated,
        }
        for dataset in datasets
    ]
    payload_json = json.dumps(payload, ensure_ascii=False)
    prelude = (
        "import io as _maigent_io\n"
        "import json as _maigent_json\n"
        f"_MAIGENT_DATASETS = {{item['id']: item for item in _maigent_json.loads({payload_json!r})}}\n"
        "\n"
        "def dataset_text(dataset_id):\n"
        "    try:\n"
        "        return _MAIGENT_DATASETS[dataset_id]['text']\n"
        "    except KeyError as exc:\n"
        "        raise KeyError(f'unknown dataset_id: {dataset_id}') from exc\n"
        "\n"
        "def dataset_meta(dataset_id):\n"
        "    try:\n"
        "        item = dict(_MAIGENT_DATASETS[dataset_id])\n"
        "    except KeyError as exc:\n"
        "        raise KeyError(f'unknown dataset_id: {dataset_id}') from exc\n"
        "    item.pop('text', None)\n"
        "    return item\n"
        "\n"
        "def load_dataset(dataset_id):\n"
        "    try:\n"
        "        item = _MAIGENT_DATASETS[dataset_id]\n"
        "    except KeyError as exc:\n"
        "        raise KeyError(f'unknown dataset_id: {dataset_id}') from exc\n"
        "    kind = item.get('kind')\n"
        "    text = item.get('text', '')\n"
        "    if kind in {'csv', 'tsv'}:\n"
        "        import pandas as _maigent_pd\n"
        "        sep = '\\t' if kind == 'tsv' else ','\n"
        "        return _maigent_pd.read_csv(_maigent_io.StringIO(text), sep=sep)\n"
        "    if kind == 'json':\n"
        "        return _maigent_json.loads(text)\n"
        "    return text\n"
        "\n"
    )
    return prelude + code


def _prepend_sandbox_runtime_prelude(code: str) -> str:
    prelude = (
        "try:\n"
        "    import matplotlib\n"
        "    matplotlib.use('Agg', force=True)\n"
        "    from matplotlib import font_manager as _maigent_font_manager\n"
        "    _maigent_japanese_font = 'Noto Sans CJK JP'\n"
        "    try:\n"
        "        _maigent_font_manager.findfont(_maigent_japanese_font, fallback_to_default=False)\n"
        "    except Exception:\n"
        "        _maigent_japanese_font = ''\n"
        "    if _maigent_japanese_font:\n"
        "        matplotlib.rcParams['font.family'] = [_maigent_japanese_font]\n"
        "        matplotlib.rcParams['axes.unicode_minus'] = False\n"
        "except Exception:\n"
        "    pass\n"
        "\n"
    )
    return prelude + code


def _tabular_dataset_shape(kind: str, text: str) -> tuple[list[str], int]:
    if kind not in {"csv", "tsv"} or not text.strip():
        return [], 0
    delimiter = "\t" if kind == "tsv" else ","
    try:
        reader = csv.reader(text.splitlines(), delimiter=delimiter)
        rows = list(reader)
    except csv.Error:
        return [], 0
    if not rows:
        return [], 0
    columns = [str(value).strip().lstrip("\ufeff") for value in rows[0]]
    row_count = max(0, len([row for row in rows[1:] if any(str(cell).strip() for cell in row)]))
    return columns, row_count


def _parse_typed_sandbox_output(output: str) -> tuple[str, list[dict[str, object]]]:
    if not output:
        return "", []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        embedded = _extract_embedded_typed_sandbox_output(output)
        if embedded:
            return embedded
        embedded = _extract_embedded_python_typed_sandbox_output(output)
        return embedded if embedded else (output, [])
    if not isinstance(payload, dict):
        return output, []
    result = _typed_sandbox_result_payload(payload)
    if result is None:
        return output, []
    stdout = str(result.get("stdout") or "")
    artifacts = result.get("artifacts", [])
    if not isinstance(artifacts, list):
        artifacts = []
    return stdout, [artifact for artifact in artifacts if isinstance(artifact, dict)]


def _extract_embedded_typed_sandbox_output(output: str) -> tuple[str, list[dict[str, object]]] | None:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(output):
        object_start = output.find("{", index)
        if object_start < 0:
            return None
        try:
            payload, object_length = decoder.raw_decode(output[object_start:])
        except json.JSONDecodeError:
            index = object_start + 1
            continue
        if isinstance(payload, dict):
            result = _typed_sandbox_result_payload(payload)
            if result is None:
                index = object_start + max(1, object_length)
                continue
            typed_stdout = str(result.get("stdout") or "").strip()
            artifacts = result.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            visible_output = (output[:object_start] + output[object_start + object_length :]).strip()
            stdout = visible_output or typed_stdout
            return stdout, [artifact for artifact in artifacts if isinstance(artifact, dict)]
        index = object_start + max(1, object_length)
    return None


def _extract_embedded_python_typed_sandbox_output(output: str) -> tuple[str, list[dict[str, object]]] | None:
    index = 0
    while index < len(output):
        object_start = output.find("{", index)
        if object_start < 0:
            return None
        literal_text = _balanced_brace_text(output, object_start)
        if not literal_text:
            index = object_start + 1
            continue
        try:
            payload = ast.literal_eval(literal_text)
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
            index = object_start + 1
            continue
        if isinstance(payload, dict):
            result = _typed_sandbox_result_payload(payload)
            if result is None:
                index = object_start + max(1, len(literal_text))
                continue
            typed_stdout = str(result.get("stdout") or "").strip()
            artifacts = result.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            visible_output = (output[:object_start] + output[object_start + len(literal_text) :]).strip()
            stdout = visible_output or typed_stdout
            return stdout, [artifact for artifact in artifacts if isinstance(artifact, dict)]
        index = object_start + max(1, len(literal_text))
    return None


def _typed_sandbox_result_payload(payload: dict[str, object]) -> dict[str, object] | None:
    result = payload.get("maigent_sandbox_result")
    if isinstance(result, dict):
        return result
    if "stdout" in payload and "artifacts" in payload:
        return payload
    return None


def _balanced_brace_text(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    quote = ""
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _looks_like_sandbox_task(message: str) -> bool:
    lowered = message.lower()
    if "```python" in lowered or lowered.startswith("/sandbox"):
        return True
    markers = [
        "計算",
        "実行",
        "python",
        "numpy",
        "pandas",
        "平均",
        "分散",
        "合計",
        "総和",
        "足し合わせ",
        "足し算",
        "標準偏差",
        "画像",
        "ヒストグラム",
        "グラフ",
        "プロット",
    ]
    if any(marker in lowered or marker in message for marker in markers):
        return True
    return bool(_extract_math_expression(message))


def _looks_like_web_search(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in ["latest", "today", "news", "current", "web search"]) or any(
        marker in message for marker in ["最新", "今日", "ニュース", "現在のWeb"]
    )


def _looks_like_rag_task(message: str) -> bool:
    lowered = message.lower()
    if re.search(r"(?:~|/)[^\s\"'<>]+", message):
        return True
    if re.search(r"[a-z0-9_.-]+\.(?:csv|tsv|txt|md|json|yaml|yml|py|html|xml|pdf|docx|xlsx|pptx)", lowered):
        return True
    return any(
        marker in lowered
        for marker in ["file", "files", "folder", "directory", "document", "docs", "list", "summarize", "read", "where"]
    ) or any(marker in message for marker in ["ファイル", "フォルダ", "資料", "一覧", "要約", "読んで", "どこ"])


def _extract_python_code(message: str) -> str:
    match = re.search(r"```python\s*(.*?)```", message, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    if message.lower().startswith("/sandbox"):
        return message[len("/sandbox") :].strip()
    return ""


def _python_for_expression(message: str) -> str:
    expression = _extract_math_expression(message)
    if not expression:
        return ""
    return (
        "import math, statistics, decimal, fractions\n"
        "allowed = {name: getattr(math, name) for name in dir(math) if not name.startswith('_')}\n"
        "allowed.update({'abs': abs, 'round': round, 'min': min, 'max': max, 'sum': sum, 'pow': pow})\n"
        f"print(eval({expression!r}, {{'__builtins__': {{}}}}, allowed))\n"
    )


def _python_for_tabular_task(message: str) -> str:
    dataset_id = _extract_first_dataset_id(message)
    csv_text = _extract_csv_context(message)
    if not csv_text and not dataset_id:
        return ""
    question = _strip_rag_context(message)
    lowered = question.lower()
    wants_mean = "平均" in question or any(marker in lowered for marker in ["mean", "average"])
    wants_variance = "分散" in question or any(marker in lowered for marker in ["variance", "var"])
    wants_std = "標準偏差" in question or any(marker in lowered for marker in ["std", "standard deviation"])
    wants_sum = any(marker in question for marker in ["合計", "総和", "足し合わせ", "足し算"]) or any(
        marker in lowered for marker in ["sum", "total", "add up"]
    )
    wants_count = any(marker in question for marker in ["件数", "個数", "データ数", "行数"]) or any(
        marker in lowered for marker in ["count", "how many"]
    )
    wants_histogram = any(marker in question for marker in ["ヒストグラム", "度数分布", "グラフ", "プロット"]) or any(
        marker in lowered for marker in ["histogram", "chart", "graph", "plot"]
    )
    if not any([wants_mean, wants_variance, wants_std, wants_sum, wants_count, wants_histogram]):
        return ""
    if dataset_id:
        source_code = (
            "import math, statistics\n"
            f"question = {question!r}\n"
            f"df = load_dataset({dataset_id!r})\n"
            "rows = df.to_dict('records') if hasattr(df, 'to_dict') else []\n"
            "headers = list(df.columns) if hasattr(df, 'columns') else []\n"
        )
    else:
        source_code = (
            "import csv, io, math, statistics\n"
            f"csv_text = {csv_text!r}\n"
            f"question = {question!r}\n"
            "reader = csv.DictReader(io.StringIO(csv_text.strip()))\n"
            "rows = list(reader)\n"
            "headers = reader.fieldnames or []\n"
        )
    return (
        source_code
        +
        "def to_number(value):\n"
        "    text = str(value).strip().replace(',', '')\n"
        "    if text.endswith('%'):\n"
        "        text = text[:-1]\n"
        "    if not text:\n"
        "        return None\n"
        "    try:\n"
        "        return float(text)\n"
        "    except ValueError:\n"
        "        return None\n"
        "numeric = {}\n"
        "for header in headers:\n"
        "    values = [to_number(row.get(header, '')) for row in rows]\n"
        "    values = [value for value in values if value is not None]\n"
        "    if values:\n"
        "        numeric[header] = values\n"
        "if not numeric:\n"
        "    raise SystemExit('CSV内に計算可能な数値列がありません。')\n"
        "question_lower = question.lower()\n"
        "positive_keywords = ['点', '点数', '得点', 'スコア', '成績', 'テスト', 'score', 'point', 'test', 'grade', 'mark']\n"
        "negative_keywords = ['id', '番号', 'no', 'num', '年', '月', '日', 'date', 'year', 'month', 'day']\n"
        "def score_column(header):\n"
        "    h = header.lower()\n"
        "    score = 0\n"
        "    for keyword in positive_keywords:\n"
        "        if keyword in header or keyword in h:\n"
        "            score += 3\n"
        "    for keyword in negative_keywords:\n"
        "        if keyword in header or keyword in h:\n"
        "            score -= 2\n"
        "    if header in question or h in question_lower:\n"
        "        score += 5\n"
        "    return score\n"
        "column = sorted(numeric, key=lambda item: (-score_column(item), headers.index(item) if item in headers else 999))[0]\n"
        "data = numeric[column]\n"
        "print(f'column: {column}')\n"
        "print(f'count: {len(data)}')\n"
        "if '平均' in question or 'mean' in question_lower or 'average' in question_lower:\n"
        "    print(f'mean: {statistics.mean(data)}')\n"
        "if '分散' in question or 'variance' in question_lower or 'var' in question_lower:\n"
        "    print(f'population_variance: {statistics.pvariance(data)}')\n"
        "    print(f'sample_variance: {statistics.variance(data) if len(data) > 1 else 0}')\n"
        "if '標準偏差' in question or 'std' in question_lower or 'standard deviation' in question_lower:\n"
        "    print(f'population_std: {statistics.pstdev(data)}')\n"
        "    print(f'sample_std: {statistics.stdev(data) if len(data) > 1 else 0}')\n"
        "if '合計' in question or '総和' in question or '足し合わせ' in question or '足し算' in question or 'sum' in question_lower or 'total' in question_lower or 'add up' in question_lower:\n"
        "    print(f'sum: {sum(data)}')\n"
        "if '件数' in question or '個数' in question or 'count' in question_lower or 'how many' in question_lower:\n"
        "    print(f'count_result: {len(data)}')\n"
        "if 'ヒストグラム' in question or '度数分布' in question or 'グラフ' in question or 'プロット' in question or 'histogram' in question_lower or 'chart' in question_lower or 'graph' in question_lower or 'plot' in question_lower:\n"
        "    bins = 10\n"
        "    low, high = min(data), max(data)\n"
        "    width = (high - low) / bins if high != low else 1\n"
        "    counts = [0] * bins\n"
        "    for value in data:\n"
        "        index = min(int((value - low) / width), bins - 1) if high != low else 0\n"
        "        counts[index] += 1\n"
        "    print('histogram:')\n"
        "    for index, count in enumerate(counts):\n"
        "        start = low + width * index\n"
        "        end = low + width * (index + 1)\n"
        "        print(f'{start:.2f}-{end:.2f}: {count}')\n"
        "    if '画像' in question or '表示' in question or 'png' in question_lower or 'image' in question_lower:\n"
        "        import base64, json, struct, zlib\n"
        "        image_width, image_height = 800, 480\n"
        "        margin_left, margin_right, margin_top, margin_bottom = 70, 30, 30, 70\n"
        "        pixels = bytearray([255, 255, 255] * image_width * image_height)\n"
        "        def set_pixel(x, y, color):\n"
        "            if 0 <= x < image_width and 0 <= y < image_height:\n"
        "                offset = (y * image_width + x) * 3\n"
        "                pixels[offset:offset + 3] = bytes(color)\n"
        "        def fill_rect(x0, y0, x1, y1, color):\n"
        "            x0, x1 = max(0, int(x0)), min(image_width, int(x1))\n"
        "            y0, y1 = max(0, int(y0)), min(image_height, int(y1))\n"
        "            for y in range(y0, y1):\n"
        "                for x in range(x0, x1):\n"
        "                    set_pixel(x, y, color)\n"
        "        axis = (40, 45, 50)\n"
        "        for x in range(margin_left, image_width - margin_right):\n"
        "            set_pixel(x, image_height - margin_bottom, axis)\n"
        "        for y in range(margin_top, image_height - margin_bottom + 1):\n"
        "            set_pixel(margin_left, y, axis)\n"
        "        plot_width = image_width - margin_left - margin_right\n"
        "        plot_height = image_height - margin_top - margin_bottom\n"
        "        max_count = max(counts) or 1\n"
        "        bar_gap = 4\n"
        "        bar_width = max(1, plot_width // bins - bar_gap)\n"
        "        for index, count in enumerate(counts):\n"
        "            x0 = margin_left + index * plot_width / bins + bar_gap / 2\n"
        "            x1 = x0 + bar_width\n"
        "            bar_height = int(plot_height * count / max_count)\n"
        "            y0 = image_height - margin_bottom - bar_height\n"
        "            y1 = image_height - margin_bottom\n"
        "            fill_rect(x0, y0, x1, y1, (52, 120, 246))\n"
        "        def chunk(kind, data_bytes):\n"
        "            return struct.pack('>I', len(data_bytes)) + kind + data_bytes + struct.pack('>I', zlib.crc32(kind + data_bytes) & 0xffffffff)\n"
        "        raw = b''.join(b'\\x00' + bytes(pixels[y * image_width * 3:(y + 1) * image_width * 3]) for y in range(image_height))\n"
        "        png = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', image_width, image_height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')\n"
        "        artifact = {'maigent_artifacts': [{'path': 'histogram.png', 'content_base64': base64.b64encode(png).decode('ascii'), 'mime_type': 'image/png', 'append': False}]}\n"
        "        print(json.dumps(artifact, ensure_ascii=False))\n"
    )


def _extract_first_dataset_id(message: str) -> str:
    match = re.search(r"^\s*-\s+id:\s+([A-Za-z0-9_-]+);", message, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _python_for_numeric_task(message: str) -> str:
    operation = _detect_numeric_operation(message)
    if not operation:
        return ""
    counts = _extract_count_targets(message)
    if operation == "count" and counts:
        return f"print({counts[0]})\n"
    numbers = _extract_numbers(message)
    if not numbers:
        return ""
    data = ", ".join(repr(number) for number in numbers)
    if operation == "sum":
        expression = "sum(data)"
    elif operation == "mean":
        expression = "statistics.mean(data)"
    elif operation == "variance":
        expression = "statistics.pvariance(data)"
    elif operation == "median":
        expression = "statistics.median(data)"
    elif operation == "stdev":
        expression = "statistics.stdev(data) if len(data) > 1 else 0"
    elif operation == "pstdev":
        expression = "statistics.pstdev(data)"
    elif operation == "min":
        expression = "min(data)"
    elif operation == "max":
        expression = "max(data)"
    elif operation == "count":
        expression = "len(data)"
    else:
        return ""
    return (
        "import statistics\n"
        f"data = [{data}]\n"
        f"print({expression})\n"
    )


def _extract_csv_context(message: str) -> str:
    blocks = re.findall(
        r"(?:File|Auto-selected file):\s+[^\n]*\.csv[^\n]*\n```text\n(.*?)\n```",
        message,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for block in blocks:
        cleaned = block.strip()
        if "," in cleaned and "\n" in cleaned:
            return cleaned
    return ""


def _extract_math_expression(message: str) -> str:
    candidates = re.findall(r"[0-9][0-9\s+\-*/().,%]*[0-9)]", message)
    for candidate in sorted(candidates, key=len, reverse=True):
        expression = candidate.replace("%", "/100")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            continue
        if _is_safe_expression(tree):
            return expression
    return ""


def _detect_numeric_operation(message: str) -> str:
    question = _strip_rag_context(message)
    lowered = question.lower()
    if any(marker in question for marker in ["標本標準偏差"]) or any(marker in lowered for marker in ["sample standard deviation", "sample std"]):
        return "stdev"
    if any(marker in question for marker in ["母標準偏差", "標準偏差"]) or any(marker in lowered for marker in ["standard deviation", "std", "pstdev"]):
        return "pstdev"
    if any(marker in question for marker in ["平均"]) or any(marker in lowered for marker in ["average", "mean"]):
        return "mean"
    if any(marker in question for marker in ["分散"]) or any(marker in lowered for marker in ["variance", "var"]):
        return "variance"
    if any(marker in question for marker in ["中央値"]) or "median" in lowered:
        return "median"
    if any(marker in question for marker in ["合計", "総和", "足し合わせ", "足し算"]) or any(
        marker in lowered for marker in ["sum", "total", "add up"]
    ):
        return "sum"
    if any(marker in question for marker in ["最大"]) or "max" in lowered:
        return "max"
    if any(marker in question for marker in ["最小"]) or "min" in lowered:
        return "min"
    if any(marker in question for marker in ["データ数", "行数", "個数", "件数", "ファイルの数", "フォルダの数"]) or any(
        marker in lowered for marker in ["count", "how many"]
    ):
        return "count"
    return ""


def _extract_count_targets(message: str) -> list[int]:
    file_count = len(re.findall(r"^\[file\]\s+", message, flags=re.MULTILINE))
    dir_count = len(re.findall(r"^\[dir\]\s+", message, flags=re.MULTILINE))
    lowered = message.lower()
    if file_count and ("ファイル" in message or "file" in lowered):
        return [file_count]
    if dir_count and ("フォルダ" in message or "ディレクトリ" in message or "directory" in lowered or "folder" in lowered):
        return [dir_count]
    return []


def _strip_rag_context(message: str) -> str:
    before_context = re.split(r"\n\s*RAG context from allowed local files:", message, maxsplit=1)[0]
    lines = _non_trace_lines(before_context)
    if not lines:
        lines = _non_trace_lines(message)
    return "\n".join(lines).strip()


def _non_trace_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("Agent plan:", "RAG result:", "Sandbox result:", "RAG search query:")):
            continue
        lines.append(line)
    return lines


def _extract_numbers(message: str) -> list[int | float]:
    values: list[int | float] = []
    for match in re.finditer(r"(?<![\w/.-])-?\d+(?:\.\d+)?(?![\w/.-])", message):
        token = match.group(0)
        try:
            value = float(token) if "." in token else int(token)
        except ValueError:
            continue
        values.append(value)
    return values


def _is_safe_expression(node: ast.AST) -> bool:
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
        ast.Load,
    )
    return all(isinstance(child, allowed) for child in ast.walk(node))

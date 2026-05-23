import ast
import logging
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import RuntimeConfig

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
    summary: str
    steps: list[AgentPlanStep]


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    output: str


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
    return False


def build_agent_plan(message: str, config: RuntimeConfig) -> AgentPlan:
    steps: list[AgentPlanStep] = []
    if _config_tool_enabled(config, "web_search") and _looks_like_web_search(message):
        steps.append(AgentPlanStep("web_search", "Collect current or external information."))
    if _config_tool_enabled(config, "rag", default=True) and _looks_like_rag_task(message):
        steps.append(AgentPlanStep("rag", "Search allowed local files with BM25 and attach relevant context."))
    if _config_tool_enabled(config, "sandbox") and _looks_like_sandbox_task(message):
        steps.append(AgentPlanStep("sandbox", "Run deterministic Python in Docker for exact computation."))
    if not steps:
        steps.append(AgentPlanStep("final", "Answer directly with the language model."))
    plan = AgentPlan(summary=_summarize_plan(steps), steps=steps)
    logger.debug("tool_plan_built summary=%s steps=%s", plan.summary, [step.tool for step in plan.steps])
    return plan


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


def run_sandbox(message: str, config: RuntimeConfig) -> SandboxResult:
    code = build_sandbox_program(message)
    if not code:
        logger.debug("sandbox_skip reason=no_code")
        return SandboxResult(False, "sandboxで実行できるPythonコードまたは計算式を特定できませんでした。")
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
    output = (completed.stdout or "") + (completed.stderr or "")
    output = output.strip() or "(no output)"
    logger.debug("sandbox_done returncode=%s output_preview=%r", completed.returncode, output[:240])
    return SandboxResult(completed.returncode == 0, output[:12000])


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
    csv_text = _extract_csv_context(message)
    if not csv_text:
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
    return (
        "import csv, io, math, statistics\n"
        f"csv_text = {csv_text!r}\n"
        f"question = {question!r}\n"
        "reader = csv.DictReader(io.StringIO(csv_text.strip()))\n"
        "rows = list(reader)\n"
        "headers = reader.fieldnames or []\n"
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
    )


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

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..config import RuntimeConfig
from ..models import AgentRun, AgentTaskRecord, AgentWorkerRun
from ..prompt_loader import load_prompt
from ..tooling import AgentPlan, AgentPlanStep, AgentWorkerResult, AgentWorkerSpec, build_agent_worker_specs
from .planning import TaskExecutionRecord


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


def _sandbox_context_dependencies(plan: AgentPlan, workers: list[AgentWorkerSpec]) -> set[str]:
    names = {worker.name for worker in workers}
    if "compute" not in names:
        return set()
    tools = [step.tool for step in plan.steps]
    try:
        sandbox_index = tools.index("sandbox")
    except ValueError:
        return set()
    dependencies: set[str] = set()
    if "research" in names:
        research_indexes = [index for index, tool in enumerate(tools) if tool in {"rag", "web_search"}]
        if research_indexes and min(research_indexes) < sandbox_index:
            dependencies.add("research")
    if "file_batch" in names:
        file_batch_indexes = [index for index, tool in enumerate(tools) if tool == "file_batch"]
        if file_batch_indexes and min(file_batch_indexes) < sandbox_index:
            dependencies.add("file_batch")
    return dependencies


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


def _truncate_db_text(text: object, limit: int = 12000) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "\n[truncated]"


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


def _format_context_worker_results(input_text: str, results: list[AgentWorkerResult]) -> str:
    lines = [input_text, "", "Context worker results for downstream tool use:"]
    for result in results:
        lines.append(f"\n[{result.name}] {result.purpose}\n{result.input_text}")
    return "\n".join(lines)


def _summarize_worker_plan(workers: list[AgentWorkerSpec]) -> str:
    return "multi-agent: " + ", ".join(f"{worker.name}({worker.role})" for worker in workers)


def _worker_order(workers: list[AgentWorkerSpec], name: str) -> int:
    for index, worker in enumerate(workers):
        if worker.name == name:
            return index
    return len(workers)

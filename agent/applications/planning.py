import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypedDict

from ..config import RuntimeConfig, load_skills
from ..models import AgentRun, Thread
from ..prompt_loader import load_prompt
from ..tooling import (
    AgentPlan,
    AgentPlanStep,
    build_agent_evaluation_criteria,
    build_agent_goal,
    build_agent_plan,
    evaluation_criterion,
)
from .llm_helpers import (
    _complete_response_with_retries,
    _control_config,
    _control_config_int,
    _control_config_max_output_tokens,
    _control_config_reasoning_effort,
    _extract_json_object,
    _log_tail,
    _tool_enabled,
)
from .rag import _build_answer_query, _has_allowed_context_sources, _search_terms, _should_search

logger = logging.getLogger("agent")

KNOWN_TOOL_NAMES = {"rag", "sandbox", "web_search", "file_batch"}
INTERNAL_PLAN_TOOL_NAMES = {"final"}


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
    local_revised_steps: list[AgentPlanStep] = []
    if _tool_enabled(config, "file_batch"):
        local_revised_steps.append(AgentPlanStep("file_batch", "Process allowed local files again with map-reduce context."))
    elif _tool_enabled(config, "rag", default=True):
        local_revised_steps.append(AgentPlanStep("rag", "Search local context again before answering."))
    if local_revised_steps:
        local_revised_steps.append(AgentPlanStep("final", "Answer with the refreshed local file context."))
        fallback = AgentPlan(
            goal=plan.goal,
            evaluation_criteria=plan.evaluation_criteria,
            summary=f"Revised local file context after failed final evaluation ({len(failed_plans) + 1}).",
            steps=local_revised_steps,
            rag_query=plan.rag_query,
        )
        logger.debug(
            "agent_plan_selection base_plan=%r selected=%r reason=fallback_local_context failed_plans=%s",
            plan.summary,
            fallback.summary,
            failed_plans,
        )
        return fallback
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
    tools = _prepare_tool_selection(thread, config)
    if not tools:
        return None
    decision = _select_tools_with_llm(config, user_text, tools)
    if not decision or not decision.steps:
        return None
    return _finalize_tool_selection_plan(thread, user_text, config, tools, decision)


def _prepare_tool_selection(thread: Thread, config) -> list[dict[str, str]] | None:
    if not _tool_selector_llm_enabled(config):
        return None
    tools = _available_tool_specs(thread, config)
    return tools or None


def _finalize_tool_selection_plan(
    thread: Thread,
    user_text: str,
    config,
    tools: list[dict[str, str]],
    decision: LlmToolPlanDecision,
) -> AgentPlan | None:
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
    if any(step.tool == "file_batch" for step in decision.steps):
        return decision
    available = {tool["name"] for tool in tools}
    if "file_batch" in available and _should_force_file_batch_for_local_context(user_text) and _has_allowed_context_sources(thread):
        steps = [AgentPlanStep("file_batch", "Process allowed local files in map-reduce batches."), *decision.steps]
        reason = f"{decision.reason}; corrected to include file_batch for folder-wide local file processing."
        logger.debug(
            "tool_selection_corrected thread_id=%s action=prepend_file_batch original_tools=%s corrected_tools=%s",
            thread.id,
            [step.tool for step in decision.steps],
            [step.tool for step in steps],
        )
        return LlmToolPlanDecision(steps=steps, rag_query=decision.rag_query, reason=reason)
    if any(step.tool == "rag" for step in decision.steps):
        return decision
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


def _should_force_file_batch_for_local_context(user_text: str) -> bool:
    lowered = user_text.lower()
    scope_markers = ["all files", "every file", "folder", "directory", "files in", "フォルダ", "ディレクトリ", "全ファイル", "すべてのファイル"]
    task_markers = ["summary", "summarize", "summaries", "table", "inventory", "要約", "一覧", "表", "読んで"]
    return any(marker in lowered or marker in user_text for marker in scope_markers) and any(
        marker in lowered or marker in user_text for marker in task_markers
    )


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


def _plan_has_actionable_tool(plan: AgentPlan | None) -> bool:
    if not plan:
        return False
    return any(step.tool in KNOWN_TOOL_NAMES or step.tool.startswith("skill:") for step in plan.steps)


def _run_precheck_in_parallel(thread: Thread, user_text: str, input_text: str, config) -> tuple[InitialClarification | None, AgentPlan | None]:
    run_clarifier = _initial_clarifier_llm_enabled(config) and not _should_skip_initial_clarifier(user_text, config)
    tools = _prepare_tool_selection(thread, config)

    clarification: InitialClarification | None = None
    decision: LlmToolPlanDecision | None = None

    # Only the pure LLM calls run inside the thread pool; DB-backed prep/finalization
    # (_prepare_tool_selection above, _finalize_tool_selection_plan below) stays on this
    # thread so it shares the request's DB connection/transaction.
    if run_clarifier and tools:
        with ThreadPoolExecutor(max_workers=2) as executor:
            clarifier_future = executor.submit(_decide_initial_clarification, config, input_text)
            decision_future = executor.submit(_select_tools_with_llm, config, user_text, tools)
            clarification = clarifier_future.result()
            decision = decision_future.result()
    elif run_clarifier:
        clarification = _decide_initial_clarification(config, input_text)
    elif tools:
        decision = _select_tools_with_llm(config, user_text, tools)

    plan: AgentPlan | None = None
    if tools and decision and decision.steps:
        plan = _finalize_tool_selection_plan(thread, user_text, config, tools, decision)

    if not clarification or not clarification.needed or not clarification.questions:
        clarification = None
    elif _plan_has_actionable_tool(plan):
        logger.debug(
            "initial_clarifier_overridden thread_id=%s reason=tool_selector_found_actionable_plan tools=%s",
            thread.id,
            [step.tool for step in plan.steps],
        )
        clarification = None

    return clarification, plan


def _should_skip_initial_clarifier(user_text: str, config) -> bool:
    text = user_text.strip()
    if not text:
        return False
    plan = build_agent_plan(text, config)
    planned_tools = {step.tool for step in plan.steps}
    actionable_tools = {"rag", "sandbox", "web_search", "file_batch"}
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
    allowed_names = {tool["name"] for tool in tools}
    instructions = _append_allowed_tool_instruction(load_prompt("tool_selection_instructions.txt"), allowed_names)
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
    steps = _parse_plan_tasks(payload.get("steps"), allowed_names)
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
    if _tool_enabled(config, "file_batch") and _has_allowed_context_sources(thread):
        tools.append(
            {
                "name": "file_batch",
                "description": "Process many allowed local files with map-reduce batches before answering; use for all-files summaries, inventories, and folder-wide analysis.",
            }
        )
    if _tool_enabled(config, "sandbox"):
        tools.append({"name": "sandbox", "description": "Run deterministic Python in Docker for calculations, code execution, data processing, or artifact generation."})
    if _tool_enabled(config, "web_search"):
        tools.append({"name": "web_search", "description": "Collect current or external web information via a configured search API (Tavily)."})
    for skill in load_skills(thread.project.path):
        description = skill.description or f"Follow the '{skill.name}' skill instructions."
        tools.append({"name": f"skill:{skill.name}", "description": description})
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


def _allowed_plan_tools(config, thread: Thread | None = None) -> set[str]:
    allowed = _allowed_configured_tool_names(config) | INTERNAL_PLAN_TOOL_NAMES
    if thread is not None:
        allowed |= {f"skill:{skill.name}" for skill in load_skills(thread.project.path)}
    return allowed


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


def _summarize_revised_plan(steps: list[AgentPlanStep], suffix: str) -> str:
    names = [step.tool for step in steps]
    if names == ["final"]:
        return f"Revised direct answer ({suffix})."
    summary_names = names if names and names[-1] == "final" else [*names, "final"]
    return " -> ".join(summary_names) + f" ({suffix})"


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

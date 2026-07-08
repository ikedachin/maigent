import json
import logging
import re
from dataclasses import dataclass

from ..openai_client import complete_response
from ..prompt_loader import load_prompt

logger = logging.getLogger("agent")


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _as_bool_like(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FinalEvaluationSettings:
    enabled: bool
    max_retries: int


def _evaluate_final_answer(config, evaluation_context: str, goal: str, evaluation_criteria: list[str], answer: str) -> dict[str, object]:
    instructions = load_prompt("final_evaluation_instructions.txt")
    prompt = load_prompt(
        "final_evaluation_prompt.txt",
        evaluation_context=evaluation_context,
        goal=goal,
        evaluation_criteria="\n".join(f"- {criterion}" for criterion in evaluation_criteria),
        answer=answer,
    )
    _log_tail("llm_prompt", prompt, config=config, purpose="final_evaluation")
    raw = _complete_response_with_retries(
        config,
        prompt,
        instructions,
        purpose="final_evaluation",
        config_name="final_evaluation",
        max_output_tokens=_final_evaluation_max_output_tokens(config),
        reasoning_effort=_final_evaluation_reasoning_effort(config),
    )
    if not raw:
        return {"adequate": False, "reason": "評価に失敗しました: LLMから空の応答が返りました。", "evaluation_failed": True}
    _log_tail("final_evaluation_raw", raw, config=config)
    normalized = _text_value(raw)["value"]
    try:
        payload = json.loads(_extract_json_object(normalized))
        return {"adequate": bool(payload.get("adequate")), "reason": str(payload.get("reason") or "")}
    except Exception:
        if normalized.lstrip().startswith("{") or "adequate" in normalized.lower():
            logger.debug("final_evaluation_parse_failed raw=%r", normalized[:240])
            return {"adequate": False, "reason": "評価結果のJSONが不完全または不正でした。", "evaluation_failed": True}
        upper = normalized.upper()
        if re.search(r"\b(INADEQUATE|FAIL|FAILED)\b", upper):
            return {"adequate": False, "reason": _extract_labeled_value(normalized, "REASON") or normalized[:240]}
        if re.search(r"\b(ADEQUATE|PASS|PASSED)\b", upper):
            return {"adequate": True, "reason": _extract_labeled_value(normalized, "REASON") or normalized[:240]}
        logger.debug("final_evaluation_parse_fallback raw=%r", normalized[:240])
        return {"adequate": False, "reason": "評価結果を解釈できませんでした。", "evaluation_failed": True}


def _text_value(text: str) -> dict[str, str]:
    return {"value": str(text or "")}


def _complete_response_with_retries(
    config,
    prompt: str,
    instructions: str,
    *,
    purpose: str,
    config_name: str,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    log_exceptions: bool = True,
) -> str:
    retries = _llm_response_max_retries(config, config_name) if max_retries is None else max_retries
    attempts = max(1, retries + 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = complete_response(
                config,
                prompt,
                instructions,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
            )
        except Exception as exc:
            last_error = str(exc)
            if log_exceptions:
                logger.exception("%s_error attempt=%s/%s", purpose, attempt, attempts)
            else:
                logger.debug("%s_error attempt=%s/%s error=%r", purpose, attempt, attempts, last_error[:240])
            continue
        raw = str(response or "").strip()
        if raw:
            if attempt > 1:
                logger.debug("%s_retry_succeeded attempt=%s/%s", purpose, attempt, attempts)
            return raw
        logger.debug("%s_empty_response attempt=%s/%s", purpose, attempt, attempts)
    if last_error:
        logger.debug("%s_failed_after_retries attempts=%s last_error=%r", purpose, attempts, last_error[:240])
    else:
        logger.debug("%s_failed_after_retries attempts=%s reason=empty_response", purpose, attempts)
    return ""


def _llm_response_max_retries(config, config_name: str, default: int = 1) -> int:
    value = _config_mapping_int(config, config_name, "llm_max_retries", None)
    if value is None:
        value = _config_mapping_int(config, config_name, "response_max_retries", None)
    if value is None and config_name != "final_evaluation":
        value = _config_mapping_int(config, config_name, "max_retries", None)
    if value is None:
        value = _config_mapping_int(config, "llm", "max_retries", default)
    return max(0, min(5, value if isinstance(value, int) else default))


def _config_mapping(config, name: str) -> dict:
    if name in {"initial_clarifier", "tool_selector", "dynamic_replanner", "dynamic_finalizer"}:
        return _control_config(config, name)
    try:
        value = getattr(config, name, {})
    except AttributeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _config_mapping_int(config, name: str, key: str, default: int | None) -> int | None:
    try:
        value = _config_mapping(config, name).get(key, default)
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _final_evaluation_max_output_tokens(config) -> int:
    value = getattr(config, "final_evaluation_max_output_tokens", 8192)
    return value if isinstance(value, int) and value > 0 else 8192


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


def _control_config(config, name: str) -> dict:
    try:
        getter = getattr(config, "control_config", None)
        if callable(getter):
            value = getter(name)
            return value if isinstance(value, dict) else {}
        direct = getattr(config, name, {})
        if isinstance(direct, dict):
            return direct
        tools = getattr(config, "tools", {})
        legacy = tools.get(name, {}) if isinstance(tools, dict) else {}
        return legacy if isinstance(legacy, dict) else {}
    except AttributeError:
        return {}


def _control_config_int(config, name: str, key: str, default: int, minimum: int = 1, maximum: int = 1024) -> int:
    try:
        value = _control_config(config, name).get(key, default)
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _control_config_max_output_tokens(config, name: str, default: int = 8192) -> int:
    try:
        value = _control_config(config, name).get("max_output_tokens", default)
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _control_config_reasoning_effort(config, name: str, key: str, default: str = "none") -> str:
    value = _control_config(config, name).get(key, default)
    if isinstance(value, bool):
        return "medium" if value else "none"
    normalized = str(value).strip().lower()
    if normalized in {"true", "on", "yes"}:
        return "medium"
    if normalized in {"false", "off", "no"}:
        return "none"
    return normalized if normalized in {"none", "minimal", "low", "medium", "high"} else default


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


def _log_tail(label: str, text: object, config=None, **fields: object) -> None:
    value = str(text or "")
    tail_chars = _llm_log_tail_chars(config)
    preview = value if tail_chars is None else value[-tail_chars:]
    metadata = " ".join(f"{key}={value!r}" for key, value in fields.items())
    if metadata:
        metadata = " " + metadata
    logger.debug("%s_tail chars=%s tail=%r%s", label, len(value), preview, metadata)


def _llm_log_tail_chars(config) -> int | None:
    if config is None:
        return 100
    value = getattr(config, "llm_log_tail_chars", 100)
    return value if value is None or isinstance(value, int) else 100

from collections.abc import Iterable

from openai import OpenAI

from .config import RuntimeConfig
from .prompt_loader import load_prompt


def generate_sandbox_code(config: RuntimeConfig, input_text: str) -> str:
    instructions = load_prompt("sandbox_code_generation_instructions.txt")
    text = _complete_response(config, input_text, instructions)
    return _extract_code(text)


def complete_response(config: RuntimeConfig, input_text: str, instructions: str = "") -> str:
    return _complete_response(config, input_text, instructions)


def stream_response(config: RuntimeConfig, input_text: str, instructions: str = "") -> Iterable[tuple[str, str]]:
    if not config.model:
        raise ValueError("モデルが未設定です。config.tomlに model または default_model を設定してください。")
    if not config.api_key:
        raise ValueError("APIキーが未設定です。config.tomlまたはOPENAI_API_KEYを設定してください。")

    kwargs = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    client = OpenAI(**kwargs)

    if config.api_mode == "chat":
        yield from _stream_chat_completions(client, config, input_text, instructions)
        return
    if config.api_mode == "responses":
        yield from _stream_responses(client, config, input_text, instructions)
        return

    try:
        yield from _stream_responses(client, config, input_text, instructions)
    except Exception:
        yield from _stream_chat_completions(client, config, input_text, instructions)


def _stream_responses(client: OpenAI, config: RuntimeConfig, input_text: str, instructions: str = "") -> Iterable[tuple[str, str]]:
    request = {
        "model": config.model,
        "input": input_text,
        "stream": True,
    }
    if instructions:
        request["instructions"] = instructions

    response_id = ""
    with client.responses.stream(**request) as stream:
        for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.created":
                response = getattr(event, "response", None)
                response_id = getattr(response, "id", "") or response_id
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    yield "delta", delta
        final = stream.get_final_response()
        response_id = getattr(final, "id", "") or response_id
    if response_id:
        yield "response_id", response_id


def _complete_response(config: RuntimeConfig, input_text: str, instructions: str = "") -> str:
    if not config.model:
        raise ValueError("モデルが未設定です。config.tomlに model または default_model を設定してください。")
    if not config.api_key:
        raise ValueError("APIキーが未設定です。config.tomlまたはOPENAI_API_KEYを設定してください。")

    kwargs = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    client = OpenAI(**kwargs)

    if config.api_mode == "chat":
        return _complete_chat_completions(client, config, input_text, instructions)
    if config.api_mode == "responses":
        return _complete_responses(client, config, input_text, instructions)
    try:
        return _complete_responses(client, config, input_text, instructions)
    except Exception:
        return _complete_chat_completions(client, config, input_text, instructions)


def _complete_responses(client: OpenAI, config: RuntimeConfig, input_text: str, instructions: str = "") -> str:
    request = {"model": config.model, "input": input_text}
    if instructions:
        request["instructions"] = instructions
    response = client.responses.create(**request)
    output_text = getattr(response, "output_text", "")
    if output_text:
        return output_text
    chunks = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", "")
            if text:
                chunks.append(text)
    return "".join(chunks)


def _complete_chat_completions(client: OpenAI, config: RuntimeConfig, input_text: str, instructions: str = "") -> str:
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    messages.append({"role": "user", "content": input_text})
    response = client.chat.completions.create(model=config.model, messages=messages, stream=False)
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", "") if message else ""


def _stream_chat_completions(client: OpenAI, config: RuntimeConfig, input_text: str, instructions: str = "") -> Iterable[tuple[str, str]]:
    messages = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    messages.append({"role": "user", "content": input_text})

    response_id = ""
    stream = client.chat.completions.create(
        model=config.model,
        messages=messages,
        stream=True,
    )
    for chunk in stream:
        response_id = getattr(chunk, "id", "") or response_id
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", "") if delta else ""
        if content:
            yield "delta", content
    if response_id:
        yield "response_id", response_id


def _extract_code(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    import re

    match = re.search(r"```(?:python)?\s*(.*?)```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped

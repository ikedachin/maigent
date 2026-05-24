import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


SENSITIVE_KEYS = {"api_key", "openai_api_key"}


@dataclass(frozen=True)
class RuntimeConfig:
    values: dict[str, Any]
    sources: list[str]

    @property
    def model(self) -> str:
        return str(self.values.get("model") or self.values.get("default_model") or "").strip()

    @property
    def api_key(self) -> str:
        return str(
            self.values.get("api_key")
            or self.values.get("openai_api_key")
            or os.environ.get("OPENAI_API_KEY", "")
        ).strip()

    @property
    def base_url(self) -> str:
        return str(
            self.values.get("base_url")
            or self.values.get("openai_base_url")
            or os.environ.get("OPENAI_BASE_URL", "")
        ).strip()

    @property
    def api_mode(self) -> str:
        value = str(self.values.get("api_mode") or self.values.get("openai_api_mode") or "auto").strip().lower()
        return value if value in {"auto", "responses", "chat"} else "auto"

    @property
    def tools(self) -> dict[str, Any]:
        tools = self.values.get("tools", {})
        return tools if isinstance(tools, dict) else {}

    @property
    def final_evaluation(self) -> dict[str, Any]:
        settings = self.values.get("final_evaluation", {})
        return settings if isinstance(settings, dict) else {}

    @property
    def final_evaluation_enabled(self) -> bool:
        return _as_bool(self.final_evaluation.get("enabled", False))

    @property
    def final_evaluation_max_retries(self) -> int:
        try:
            return max(0, min(3, int(self.final_evaluation.get("max_retries", 3))))
        except (TypeError, ValueError):
            return 3

    def tool_enabled(self, name: str, default: bool = False) -> bool:
        config = self.tools.get(name, {})
        if not isinstance(config, dict):
            return default
        return _as_bool(config.get("enabled", default))

    @property
    def sandbox_image(self) -> str:
        sandbox = self.tools.get("sandbox", {})
        if not isinstance(sandbox, dict):
            return "python:3.11-slim"
        return str(sandbox.get("image") or "python:3.11-slim").strip()

    @property
    def sandbox_allowed_libraries(self) -> list[str]:
        sandbox = self.tools.get("sandbox", {})
        if not isinstance(sandbox, dict):
            return []
        libraries = sandbox.get("allowed_libraries", [])
        if isinstance(libraries, str):
            return [libraries]
        if isinstance(libraries, list):
            return [str(item).strip() for item in libraries if str(item).strip()]
        return []

    @property
    def sandbox_install_libraries_on_run(self) -> bool:
        sandbox = self.tools.get("sandbox", {})
        if not isinstance(sandbox, dict):
            return False
        return _as_bool(sandbox.get("install_libraries_on_run", False))

    @property
    def sandbox_timeout_seconds(self) -> int:
        sandbox = self.tools.get("sandbox", {})
        if not isinstance(sandbox, dict):
            return 20
        try:
            return max(1, min(600, int(sandbox.get("timeout_seconds", 20))))
        except (TypeError, ValueError):
            return 20

    def redacted(self) -> dict[str, Any]:
        safe = dict(self.values)
        for key in SENSITIVE_KEYS:
            if key in safe and safe[key]:
                safe[key] = "********"
        if os.environ.get("OPENAI_API_KEY") and not any(k in self.values for k in SENSITIVE_KEYS):
            safe["api_key"] = "******** (env)"
        return safe


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def _read_yaml_subset(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending_key: tuple[int, dict[str, Any], str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if stripped.startswith("- "):
            value = _parse_scalar(stripped[2:].strip())
            if not isinstance(parent, list):
                if pending_key is None:
                    continue
                pending_indent, pending_parent, key = pending_key
                if pending_indent != indent:
                    continue
                new_list: list[Any] = []
                pending_parent[key] = new_list
                stack.append((indent - 1, new_list))
                parent = new_list
            parent.append(value)
            continue
        if ":" not in stripped or not isinstance(parent, dict):
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            parent[key] = _parse_scalar(value)
            pending_key = None
            continue
        child: dict[str, Any] = {}
        parent[key] = child
        stack.append((indent, child))
        pending_key = (indent + 2, parent, key)
    return root


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _deep_update(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_config_pair(directory: Path) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    sources: list[str] = []
    toml_path = directory / "config.toml"
    toml_values = _read_toml(toml_path)
    if toml_values:
        _deep_update(values, toml_values)
        sources.append(str(toml_path))
    yaml_path = directory / "config.yaml"
    yaml_values = _read_yaml_subset(yaml_path)
    if yaml_values:
        _deep_update(values, yaml_values)
        sources.append(str(yaml_path))
    return values, sources


def load_runtime_config(project_path: str = "") -> RuntimeConfig:
    values: dict[str, Any] = {}
    sources: list[str] = []

    user_values, user_sources = _load_config_pair(Path.home() / ".maigent")
    if user_values:
        _deep_update(values, user_values)
        sources.extend(user_sources)

    app_dir = Path(settings.BASE_DIR) / ".maigent"
    app_values, app_sources = _load_config_pair(app_dir)
    if app_values:
        _deep_update(values, app_values)
        sources.extend(app_sources)

    if project_path:
        project_dir = Path(project_path).expanduser() / ".maigent"
        if project_dir != app_dir:
            project_values, project_sources = _load_config_pair(project_dir)
            if project_values:
                _deep_update(values, project_values)
                sources.extend(project_sources)

    env_model = os.environ.get("OPENAI_MODEL")
    if env_model and not values.get("model") and not values.get("default_model"):
        values["model"] = env_model
        sources.append("OPENAI_MODEL")

    return RuntimeConfig(values=values, sources=sources)

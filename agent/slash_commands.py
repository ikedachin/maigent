from pathlib import Path

from django.utils import timezone

from .access import is_path_allowed
from .config import RuntimeConfig
from .models import FeatureFlag, Message, Thread

MAX_READ_CHARS = 12000


def handle_slash_command(thread: Thread, command_text: str, config: RuntimeConfig) -> str:
    parts = command_text.strip().split()
    command = parts[0].lower() if parts else ""

    if command == "/status":
        flags = FeatureFlag.objects.order_by("name")
        flag_text = ", ".join(f"{flag.name}={'on' if flag.enabled else 'off'}" for flag in flags) or "none"
        sources = ", ".join(config.sources) or "none"
        return "\n".join(
            [
                f"Project: {thread.project.name}",
                f"Thread: {thread.title}",
                f"Model: {config.model or '未設定'}",
                f"Config sources: {sources}",
                f"Feature flags: {flag_text}",
            ]
        )

    if command == "/model":
        return f"Current model: {config.model or '未設定'}"

    if command == "/read":
        return _handle_read(thread, parts)

    if command == "/ls":
        return _handle_ls(thread, parts)

    if command in {"/file", "/files"}:
        return _handle_file(thread, parts)

    if command == "/compact":
        messages = thread.messages.order_by("-created_at")[:12]
        lines = [f"{message.role}: {message.content[:180]}" for message in reversed(list(messages))]
        thread.summary = "Updated at {:%Y-%m-%d %H:%M}\n{}".format(timezone.localtime(), "\n".join(lines))
        thread.save(update_fields=["summary", "updated_at"])
        return "スレッド要約を更新しました。"

    if command == "/resume":
        threads = Thread.objects.filter(project=thread.project).order_by("-updated_at")[:8]
        return "再開可能なスレッド:\n" + "\n".join(f"- #{item.id} {item.title}" for item in threads)

    if command == "/fork":
        fork = Thread.objects.create(
            project=thread.project,
            title=f"{thread.title} fork",
            memory_enabled=thread.memory_enabled,
            summary=thread.summary,
        )
        for message in thread.messages.all():
            Message.objects.create(thread=fork, role=message.role, content=message.content, status=message.status)
        return f"新しいスレッド #{fork.id} に分岐しました。"

    if command == "/features":
        return _handle_features(parts)

    if command == "/memories":
        thread.memory_enabled = not thread.memory_enabled
        thread.save(update_fields=["memory_enabled", "updated_at"])
        return f"Memories: {'on' if thread.memory_enabled else 'off'}"

    if command in {"/experimental", "/agent", "/theme", "/apps"}:
        return f"{command} は初回版では状態表示のみです。設定パネルから管理してください。"

    return f"未対応のスラッシュコマンドです: {command}"


def _handle_read(thread: Thread, parts: list[str]) -> str:
    if len(parts) < 2:
        return "使い方: /read <file-path>"
    path = " ".join(parts[1:])
    target = Path(path).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        return f"読み取りに失敗しました: {exc}"
    if not is_path_allowed(thread.project, str(resolved), write=False):
        return "読み取りが許可されていません。右側のアクセス許可に、このファイルまたは親フォルダを追加してください。"
    if not resolved.exists():
        return f"ファイルが存在しません: {resolved}"
    if not resolved.is_file():
        return f"ファイルではありません: {resolved}"
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "UTF-8テキストとして読めませんでした。現在はテキストファイルのみ対応しています。"
    except OSError as exc:
        return f"読み取りに失敗しました: {exc}"
    truncated = len(content) > MAX_READ_CHARS
    if truncated:
        content = content[:MAX_READ_CHARS]
    suffix = "\n\n[出力は先頭部分のみです。]" if truncated else ""
    return f"File: {resolved}\n\n```text\n{content}\n```{suffix}"


def _handle_file(thread: Thread, parts: list[str]) -> str:
    if len(parts) >= 2:
        target = Path(" ".join(parts[1:])).expanduser()
        try:
            resolved = target.resolve()
        except OSError as exc:
            return f"ファイル確認に失敗しました: {exc}"
        if resolved.is_dir():
            return _handle_ls(thread, ["/ls", str(resolved)])
        return _handle_read(thread, ["/read", str(resolved)])

    access_paths = thread.project.access_paths.filter(mode__in=["read", "write"])
    if not access_paths:
        return "許可済みファイル/フォルダはありません。右側のアクセス許可から追加してください。"
    lines = ["許可済みファイル/フォルダ:"]
    for access in access_paths:
        label = "読み書き" if access.mode == "write" else "読み取り"
        lines.append(f"- [{label}] {access.path}")
    lines.append("")
    lines.append("使い方: /file <path> または /read <file-path> / /ls <folder-path>")
    return "\n".join(lines)


def _handle_ls(thread: Thread, parts: list[str]) -> str:
    if len(parts) < 2:
        return "使い方: /ls <folder-path>"
    path = " ".join(parts[1:])
    target = Path(path).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        return f"一覧取得に失敗しました: {exc}"
    if not is_path_allowed(thread.project, str(resolved), write=False):
        return "読み取りが許可されていません。右側のアクセス許可に、このフォルダまたは親フォルダを追加してください。"
    if not resolved.exists():
        return f"フォルダが存在しません: {resolved}"
    if not resolved.is_dir():
        return f"フォルダではありません: {resolved}"
    try:
        children = sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:80]
    except OSError as exc:
        return f"一覧取得に失敗しました: {exc}"
    lines = [f"{'[dir]' if child.is_dir() else '[file]'} {child.name}" for child in children]
    return f"Folder: {resolved}\n" + ("\n".join(lines) if lines else "(empty)")


def _handle_features(parts: list[str]) -> str:
    action = parts[1].lower() if len(parts) > 1 else "list"
    if action == "list":
        flags = FeatureFlag.objects.order_by("name")
        return "Feature flags:\n" + ("\n".join(f"- {flag.name}: {'enabled' if flag.enabled else 'disabled'}" for flag in flags) or "- none")
    if action in {"enable", "disable"} and len(parts) >= 3:
        flag, _ = FeatureFlag.objects.get_or_create(name=parts[2])
        flag.enabled = action == "enable"
        flag.save(update_fields=["enabled", "updated_at"])
        return f"{flag.name}: {'enabled' if flag.enabled else 'disabled'}"
    return "使い方: /features list | /features enable <name> | /features disable <name>"

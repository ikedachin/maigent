import base64
import binascii
from dataclasses import dataclass
from pathlib import Path

from .models import FeatureFlag, Project

FILE_WRITE_FEATURE_FLAG = "file_write"
MAX_BROKER_WRITE_CHARS = 200_000
MAX_BROKER_BINARY_BYTES = 5_000_000
IMAGE_MIME_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class BrokerWriteResult:
    ok: bool
    message: str
    path: str = ""
    chars: int = 0


def file_write_enabled() -> bool:
    flag = FeatureFlag.objects.filter(name=FILE_WRITE_FEATURE_FLAG).first()
    return True if flag is None else flag.enabled


def _resolve_project_output_target(project: Project, path_text: str) -> tuple[Path | None, str]:
    output_path = (project.output_path or "").strip()
    if not output_path:
        return None, "書き出し先フォルダが未設定です。右側の書き出し先で保存先フォルダを選択してください。"
    try:
        output_root = Path(output_path).expanduser().resolve()
    except OSError as exc:
        return None, f"書き出し先フォルダの確認に失敗しました: {exc}"
    if not output_root.exists():
        return None, f"書き出し先フォルダが存在しません: {output_root}"
    if not output_root.is_dir():
        return None, f"書き出し先がフォルダではありません: {output_root}"

    raw_target = Path(path_text).expanduser()
    try:
        resolved = raw_target.resolve() if raw_target.is_absolute() else (output_root / raw_target).resolve()
    except OSError as exc:
        return None, f"書き込み先の確認に失敗しました: {exc}"
    if resolved != output_root and output_root not in resolved.parents:
        return None, f"書き込みが許可されていません。書き出し先フォルダ配下のみ保存できます: {output_root}"
    return resolved, ""


def write_allowed_text_file(project: Project, path_text: str, content: str, append: bool = False) -> BrokerWriteResult:
    if not file_write_enabled():
        return BrokerWriteResult(
            False,
            "ファイル書き込み機能が無効です。右側の機能フラグで file_write を有効化してください。",
        )
    if len(content) > MAX_BROKER_WRITE_CHARS:
        return BrokerWriteResult(False, f"書き込み内容が大きすぎます。上限は {MAX_BROKER_WRITE_CHARS} 文字です。")
    resolved, error = _resolve_project_output_target(project, path_text)
    if error:
        return BrokerWriteResult(False, error)
    if resolved is None:
        return BrokerWriteResult(False, "書き込み先を解決できませんでした。")
    if resolved.exists() and resolved.is_dir():
        return BrokerWriteResult(False, f"書き込み先がフォルダです: {resolved}")
    if not resolved.parent.exists():
        return BrokerWriteResult(False, f"親フォルダが存在しません: {resolved.parent}")
    if not resolved.parent.is_dir():
        return BrokerWriteResult(False, f"親パスがフォルダではありません: {resolved.parent}")
    try:
        if append:
            with resolved.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return BrokerWriteResult(False, f"書き込みに失敗しました: {exc}")
    action = "追記" if append else "書き込み"
    return BrokerWriteResult(True, f"{action}しました: {resolved}\n文字数: {len(content)}", str(resolved), len(content))


def allowed_image_mime_type(path_text: str) -> str:
    return IMAGE_MIME_TYPES.get(Path(path_text).suffix.lower(), "")


def write_allowed_binary_file(project: Project, path_text: str, content_base64: str, append: bool = False) -> BrokerWriteResult:
    if not file_write_enabled():
        return BrokerWriteResult(
            False,
            "ファイル書き込み機能が無効です。右側の機能フラグで file_write を有効化してください。",
        )
    if append:
        return BrokerWriteResult(False, "バイナリ成果物への追記はできません。")
    mime_type = allowed_image_mime_type(path_text)
    if not mime_type:
        return BrokerWriteResult(False, "画像成果物は png, jpg, jpeg, webp, gif のみ保存できます。")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        return BrokerWriteResult(False, "画像成果物のbase64デコードに失敗しました。")
    if len(data) > MAX_BROKER_BINARY_BYTES:
        return BrokerWriteResult(False, f"画像成果物が大きすぎます。上限は {MAX_BROKER_BINARY_BYTES} バイトです。")
    resolved, error = _resolve_project_output_target(project, path_text)
    if error:
        return BrokerWriteResult(False, error)
    if resolved is None:
        return BrokerWriteResult(False, "書き込み先を解決できませんでした。")
    if resolved.exists() and resolved.is_dir():
        return BrokerWriteResult(False, f"書き込み先がフォルダです: {resolved}")
    if not resolved.parent.exists():
        return BrokerWriteResult(False, f"親フォルダが存在しません: {resolved.parent}")
    if not resolved.parent.is_dir():
        return BrokerWriteResult(False, f"親パスがフォルダではありません: {resolved.parent}")
    try:
        resolved.write_bytes(data)
    except OSError as exc:
        return BrokerWriteResult(False, f"書き込みに失敗しました: {exc}")
    return BrokerWriteResult(True, f"書き込みました: {resolved}\nバイト数: {len(data)}", str(resolved), len(data))


def resolve_project_output_file(project: Project, path_text: str) -> tuple[Path | None, str]:
    return _resolve_project_output_target(project, path_text)

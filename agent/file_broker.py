from dataclasses import dataclass
from pathlib import Path

from .access import is_path_allowed
from .models import FeatureFlag, Project

FILE_WRITE_FEATURE_FLAG = "file_write"
MAX_BROKER_WRITE_CHARS = 200_000


@dataclass(frozen=True)
class BrokerWriteResult:
    ok: bool
    message: str
    path: str = ""
    chars: int = 0


def file_write_enabled() -> bool:
    flag = FeatureFlag.objects.filter(name=FILE_WRITE_FEATURE_FLAG).first()
    return True if flag is None else flag.enabled


def write_allowed_text_file(project: Project, path_text: str, content: str, append: bool = False) -> BrokerWriteResult:
    if not file_write_enabled():
        return BrokerWriteResult(
            False,
            "ファイル書き込み機能が無効です。右側の機能フラグで file_write を有効化してください。",
        )
    if len(content) > MAX_BROKER_WRITE_CHARS:
        return BrokerWriteResult(False, f"書き込み内容が大きすぎます。上限は {MAX_BROKER_WRITE_CHARS} 文字です。")
    target = Path(path_text).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        return BrokerWriteResult(False, f"書き込み先の確認に失敗しました: {exc}")
    if not is_path_allowed(project, str(resolved), write=True):
        return BrokerWriteResult(False, "書き込みが許可されていません。右側のアクセス許可に、このファイルまたは親フォルダを読み書きで追加してください。")
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

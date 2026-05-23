from pathlib import Path

from .models import Project


def normalize_access_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def is_path_allowed(project: Project, candidate: str, write: bool = False) -> bool:
    try:
        target = Path(candidate).expanduser().resolve()
    except OSError:
        return False
    modes = ["write"] if write else ["read", "write"]
    for access in project.access_paths.filter(mode__in=modes):
        try:
            root = Path(access.path).expanduser().resolve()
        except OSError:
            continue
        if target == root or root in target.parents:
            return True
    return False

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"


def load_prompt(name: str, **values: object) -> str:
    text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text.strip()

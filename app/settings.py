"""Process configuration. A blank Groq key means no chat model."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_DATABASE_PATH = "data/assistant.db"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True, repr=False)
class Settings:
    groq_api_key: str
    groq_model: str
    groq_base_url: str
    database_path: Path
    embedding_model: str

    @property
    def chat_enabled(self) -> bool:
        return bool(self.groq_api_key)

    def __repr__(self) -> str:
        key_state = "set" if self.groq_api_key else "empty"
        return (
            "Settings("
            f"groq_api_key={key_state}, "
            f"groq_model={self.groq_model!r}, "
            f"groq_base_url={self.groq_base_url!r}, "
            f"database_path={str(self.database_path)!r}, "
            f"embedding_model={self.embedding_model!r})"
        )


def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    database_path = Path(_env("DATABASE_PATH", DEFAULT_DATABASE_PATH))
    if not database_path.is_absolute():
        database_path = ROOT / database_path
    return Settings(
        groq_api_key=_env("GROQ_API_KEY"),
        groq_model=_env("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        groq_base_url=_env("GROQ_BASE_URL", DEFAULT_GROQ_BASE_URL).rstrip("/"),
        database_path=database_path,
        embedding_model=_env("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
    )

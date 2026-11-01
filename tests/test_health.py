"""Phase 1 settings and disclaimer. Health scheme count comes from the Phase 3 seed."""

import sys
from pathlib import Path

from app.settings import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_GROQ_BASE_URL,
    DEFAULT_GROQ_MODEL,
    ROOT,
    get_settings,
)


def test_health_reports_seeded_schemes_before_ingest(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "schemes": 10,
        "documents": 0,
        "last_ingest": None,
    }


def test_index_shows_disclaimer(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Facts-only. No investment advice." in response.text


def test_main_does_not_import_sentence_transformers():
    assert "sentence_transformers" not in sys.modules


def test_env_example_has_empty_groq_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "GROQ_BASE_URL",
        "DATABASE_PATH",
        "EMBEDDING_MODEL",
    ):
        assert f"{name}=" in text
    assert "OPENAI_" not in text
    for line in text.splitlines():
        if line.startswith("GROQ_") or line.startswith("DATABASE_") or line.startswith("EMBEDDING_"):
            assert line.endswith("=")


def test_gitignore_covers_env_data_and_venv():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (".venv/", "data/", ".env", ".cache/"):
        assert entry in text


def test_blank_groq_key_disables_chat(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GROQ_MODEL", "")
    monkeypatch.setenv("GROQ_BASE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    settings = get_settings()
    assert settings.chat_enabled is False
    assert settings.groq_model == DEFAULT_GROQ_MODEL
    assert settings.groq_base_url == DEFAULT_GROQ_BASE_URL
    assert settings.database_path == ROOT / "data" / "assistant.db"
    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert "empty" in repr(settings)


def test_repr_hides_groq_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_value")
    settings = get_settings()
    assert settings.chat_enabled is True
    rendered = repr(settings)
    assert "gsk_test_secret_value" not in rendered
    assert "groq_api_key=set" in rendered


def test_relative_database_path_stays_inside_the_project(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_PATH", "data/other.db")
    settings = get_settings()
    assert settings.database_path == ROOT / "data" / "other.db"
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "assistant.db"))
    absolute = get_settings()
    assert absolute.database_path == tmp_path / "assistant.db"

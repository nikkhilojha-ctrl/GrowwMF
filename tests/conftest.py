"""Each test that talks to the API gets its own SQLite file."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(autouse=True)
def _blank_groq_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests do not call Groq. A case that needs a key sets it after this."""
    monkeypatch.setenv("GROQ_API_KEY", "")


@pytest.fixture
def database_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "assistant.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    return path


@pytest.fixture
def client(database_path: Path):
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client

"""Phase 3: scheme seed, empty corpus tables, and isolated threads."""

import re
import sqlite3
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.db import connect, transaction
from app.main import create_app
from app.settings import ROOT


def test_create_thread_returns_a_uuid_and_the_default_title(client):
    response = client.post("/api/threads")
    assert response.status_code == 200
    body = response.json()
    uuid.UUID(body["thread_id"])
    assert body["title"] == "New conversation"
    assert body["created_at"]


def test_two_threads_keep_their_own_messages(client):
    first = client.post("/api/threads").json()
    second = client.post("/api/threads").json()
    assert client.post(
        f"/api/threads/{first['thread_id']}/messages",
        json={"text": "marker one"},
    ).status_code == 200
    posted = client.post(
        f"/api/threads/{second['thread_id']}/messages",
        json={"text": "marker two"},
    )
    assert posted.status_code == 200
    assert posted.json()["kind"] == "clarify_scheme"
    assert posted.json()["scheme_id"] is None
    assert "marker two" not in posted.json()["text"]
    assert "marker one" not in posted.json()["text"]

    got_first = client.get(f"/api/threads/{first['thread_id']}")
    got_second = client.get(f"/api/threads/{second['thread_id']}")
    assert got_first.status_code == 200
    assert got_second.status_code == 200
    first_body = got_first.json()
    second_body = got_second.json()
    assert first_body["active_scheme_id"] is None
    assert second_body["active_scheme_id"] is None
    assert [message["text"] for message in first_body["messages"] if message["role"] == "user"] == [
        "marker one"
    ]
    assert [message["text"] for message in second_body["messages"] if message["role"] == "user"] == [
        "marker two"
    ]
    assert first_body["messages"][0]["role"] == "user"
    assert first_body["messages"][0]["kind"] is None
    assert first_body["messages"][1]["role"] == "assistant"
    assert first_body["messages"][1]["kind"] == "clarify_scheme"
    assert "marker two" not in first_body["messages"][1]["text"]
    assert "marker one" not in second_body["messages"][1]["text"]
    assert "placeholder" not in first_body["messages"][1]["kind"]


def test_threads_are_listed_newest_first(client):
    older = client.post("/api/threads").json()
    newer = client.post("/api/threads").json()
    listing = client.get("/api/threads")
    assert listing.status_code == 200
    ids = [row["thread_id"] for row in listing.json()]
    assert ids[0] == newer["thread_id"]
    assert ids[1] == older["thread_id"]
    assert listing.json()[0]["active_scheme_id"] is None

    client.post(
        f"/api/threads/{older['thread_id']}/messages",
        json={"text": "bump the older thread"},
    )
    refreshed = [row["thread_id"] for row in client.get("/api/threads").json()]
    assert refreshed[0] == older["thread_id"]


def test_unknown_thread_returns_404_and_writes_nothing(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/threads/{missing}").status_code == 404
    assert client.get("/api/threads/not-a-uuid").status_code == 404
    before = client.get("/api/threads").json()
    posted = client.post(f"/api/threads/{missing}/messages", json={"text": "hello"})
    assert posted.status_code == 404
    assert client.get("/api/threads").json() == before
    assert client.post("/api/threads/not-a-uuid/messages", json={"text": "hello"}).status_code == 404


def test_empty_and_invalid_text_return_422_and_write_nothing(client):
    created = client.post("/api/threads").json()
    url = f"/api/threads/{created['thread_id']}/messages"
    for payload in ({}, {"text": ""}, {"text": "  \n"}, {"text": None}, {"text": 12}, {"text": {"nope": True}}):
        response = client.post(url, json=payload)
        assert response.status_code == 422
    assert client.get(f"/api/threads/{created['thread_id']}").json()["messages"] == []


def test_text_over_1000_characters_returns_413_and_writes_nothing(client):
    created = client.post("/api/threads").json()
    url = f"/api/threads/{created['thread_id']}/messages"
    accepted = client.post(url, json={"text": "y" * 1000})
    assert accepted.status_code == 200
    stored = client.get(f"/api/threads/{created['thread_id']}").json()["messages"]
    assert stored[0]["text"] == "y" * 1000

    other = client.post("/api/threads").json()
    rejected = client.post(
        f"/api/threads/{other['thread_id']}/messages",
        json={"text": "x" * 1001},
    )
    assert rejected.status_code == 413
    assert client.get(f"/api/threads/{other['thread_id']}").json()["messages"] == []


def test_extra_json_fields_are_ignored(client):
    created = client.post("/api/threads").json()
    response = client.post(
        f"/api/threads/{created['thread_id']}/messages",
        json={"text": "hello", "scheme_id": "uti-elss", "kind": "answer"},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/threads/{created['thread_id']}").json()
    assert detail["active_scheme_id"] is None
    assert detail["messages"][0]["text"] == "hello"
    assert detail["messages"][1]["kind"] == "clarify_scheme"
    assert detail["messages"][1]["text"] != "hello"


def test_thread_lookup_uses_the_id_as_a_value(client):
    created = client.post("/api/threads").json()
    client.post(
        f"/api/threads/{created['thread_id']}/messages",
        json={"text": "secret-alpha"},
    )
    needle = quote("' OR thread_id IS NOT NULL --")
    assert client.get(f"/api/threads/{needle}").status_code == 404
    detail = client.get(f"/api/threads/{created['thread_id']}").json()
    assert detail["messages"][0]["text"] == "secret-alpha"


def test_overlapping_posts_do_not_cross_threads(database_path: Path):
    setup = create_app()
    with TestClient(setup) as starter:
        thread_a = starter.post("/api/threads").json()["thread_id"]
        thread_b = starter.post("/api/threads").json()["thread_id"]

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def post(thread_id: str, text: str) -> None:
        try:
            application = create_app()
            with TestClient(application) as local:
                barrier.wait(timeout=5)
                response = local.post(
                    f"/api/threads/{thread_id}/messages",
                    json={"text": text},
                )
                if response.status_code != 200:
                    raise AssertionError(response.status_code)
        except BaseException as exc:  # pragma: no cover - failure path for the assertion below
            errors.append(exc)

    workers = [
        threading.Thread(target=post, args=(thread_a, "thread-a-only")),
        threading.Thread(target=post, args=(thread_b, "thread-b-only")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []

    reader = create_app()
    with TestClient(reader) as restarted:
        transcript_a = restarted.get(f"/api/threads/{thread_a}").json()
        transcript_b = restarted.get(f"/api/threads/{thread_b}").json()
    assert [message["text"] for message in transcript_a["messages"] if message["role"] == "user"] == [
        "thread-a-only"
    ]
    assert [message["text"] for message in transcript_b["messages"] if message["role"] == "user"] == [
        "thread-b-only"
    ]
    assert transcript_a["active_scheme_id"] is None
    assert transcript_b["active_scheme_id"] is None


def test_threads_survive_a_new_process(database_path: Path):
    thread_id = ""
    first = create_app()
    with TestClient(first) as starter:
        created = starter.post("/api/threads").json()
        thread_id = created["thread_id"]
        assert starter.post(
            f"/api/threads/{thread_id}/messages",
            json={"text": "still here"},
        ).status_code == 200

    second = create_app()
    with TestClient(second) as restarted:
        listing = restarted.get("/api/threads").json()
        detail = restarted.get(f"/api/threads/{thread_id}").json()
    assert listing[0]["thread_id"] == thread_id
    assert detail["title"] == "New conversation"
    assert detail["active_scheme_id"] is None
    assert detail["messages"][0]["text"] == "still here"
    assert detail["messages"][1]["role"] == "assistant"
    assert detail["messages"][1]["kind"] == "clarify_scheme"


def test_schema_seeds_schemes_and_leaves_chunks_empty(client, database_path: Path):
    conn = connect(database_path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert {
            "schemes",
            "chunks",
            "chunks_fts",
            "scheme_facts",
            "threads",
            "messages",
        } <= names
        assert conn.execute("SELECT COUNT(*) FROM schemes").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheme_facts").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_chunk_embedding_and_scheme_may_be_null(database_path: Path, client):
    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO chunks (
                chunk_id, url, scheme_id, doc_type, title, page, heading, text, retrieved_at, embedding
            ) VALUES (?, ?, NULL, 'amfi', 'title', NULL, NULL, 'body', '2026-10-25T00:00:00Z', NULL)
            """,
            ("chunk-1", "https://www.amfiindia.com/example"),
        )
        row = conn.execute(
            "SELECT scheme_id, embedding FROM chunks WHERE chunk_id = ?",
            ("chunk-1",),
        ).fetchone()
        assert row["scheme_id"] is None
        assert row["embedding"] is None
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    finally:
        conn.close()


def test_foreign_keys_reject_orphan_messages_and_cascade_deletes(database_path: Path, client):
    created = client.post("/api/threads").json()
    client.post(
        f"/api/threads/{created['thread_id']}/messages",
        json={"text": "goes away with the thread"},
    )
    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, thread_id, role, kind, text, citation_url, footer, created_at
                ) VALUES ('orphan', 'missing-thread', 'user', NULL, 'nope', NULL, NULL, ?)
                """,
                ("2026-10-25T00:00:00Z",),
            )
        conn.execute("ROLLBACK")

        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO chunks (
                    chunk_id, url, scheme_id, doc_type, title, text, retrieved_at
                ) VALUES ('bad', 'https://www.utimf.com/x', 'missing-scheme', 'sid', 't', 'body', ?)
                """,
                ("2026-10-25T00:00:00Z",),
            )
        conn.execute("ROLLBACK")

        conn.execute("DELETE FROM threads WHERE thread_id = ?", (created["thread_id"],))
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    finally:
        conn.close()


def test_message_sql_is_scoped_to_one_thread():
    source = (ROOT / "app" / "threads.py").read_text(encoding="utf-8")
    blocks = re.findall(r'"""(.*?)"""', source, flags=re.DOTALL)
    message_sql = [
        block
        for block in blocks
        if re.search(r"\b(FROM|INTO|UPDATE|JOIN)\s+messages\b", block, flags=re.IGNORECASE)
    ]
    assert message_sql
    for block in message_sql:
        assert "thread_id" in block
        if re.search(r"\b(SELECT|UPDATE|DELETE)\b", block, flags=re.IGNORECASE):
            assert re.search(r"WHERE\s+thread_id\s*=\s*\?", block, flags=re.IGNORECASE)
    assert re.search(
        r"UPDATE\s+threads\s+SET\s+title\s*=\s*\?,\s*active_scheme_id\s*=\s*\?,\s*updated_at\s*=\s*\?\s+WHERE\s+thread_id\s*=\s*\?",
        source,
        flags=re.IGNORECASE,
    )


RETRIEVED = "2026-01-15T09:00:00.000000Z"
NIFTY_TER = "Direct plan TER 0.18%"
ELSS_TER = "Direct plan TER 1.02%"


def _fact(path: Path, scheme_id: str, value: str, url: str) -> None:
    with transaction(path) as conn:
        conn.execute(
            """
            INSERT INTO scheme_facts (
                scheme_id, field, value_text, source_url, page, retrieved_at
            ) VALUES (?, 'expense_ratio', ?, ?, NULL, ?)
            """,
            (scheme_id, value, url, RETRIEVED),
        )


def test_two_threads_keep_their_own_expense_ratios(client, database_path: Path):
    from app.schemes import load_corpus

    corpus = load_corpus()
    nifty = corpus.scheme("uti-nifty")
    elss = corpus.scheme("uti-elss")
    _fact(database_path, "uti-nifty", NIFTY_TER, nifty.factsheet_url)
    _fact(database_path, "uti-elss", ELSS_TER, elss.factsheet_url)
    thread_a = client.post("/api/threads").json()["thread_id"]
    thread_b = client.post("/api/threads").json()["thread_id"]

    first = client.post(
        f"/api/threads/{thread_a}/messages",
        json={"text": "What is the expense ratio of UTI Long Term Equity Fund?"},
    )
    second = client.post(
        f"/api/threads/{thread_b}/messages",
        json={"text": "What is the expense ratio of UTI Nifty Fund?"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["kind"] == "answer"
    assert first.json()["scheme_id"] == "uti-elss"
    assert elss.name in first.json()["text"]
    assert ELSS_TER in first.json()["text"]
    assert NIFTY_TER not in first.json()["text"]
    assert first.json()["citation"]["url"] == elss.factsheet_url
    assert second.json()["scheme_id"] == "uti-nifty"
    assert nifty.name in second.json()["text"]
    assert NIFTY_TER in second.json()["text"]
    assert ELSS_TER not in second.json()["text"]
    assert second.json()["citation"]["url"] == nifty.factsheet_url

    follow = client.post(
        f"/api/threads/{thread_a}/messages",
        json={"text": "What is the expense ratio?"},
    )
    assert follow.status_code == 200
    assert follow.json()["scheme_id"] == "uti-elss"
    assert ELSS_TER in follow.json()["text"]
    assert NIFTY_TER not in follow.json()["text"]
    assert follow.json()["citation"]["url"] == elss.factsheet_url

    transcript_a = client.get(f"/api/threads/{thread_a}").json()
    transcript_b = client.get(f"/api/threads/{thread_b}").json()
    assert transcript_a["active_scheme_id"] == "uti-elss"
    assert transcript_b["active_scheme_id"] == "uti-nifty"
    assert NIFTY_TER not in " ".join(message["text"] for message in transcript_a["messages"])
    assert ELSS_TER not in " ".join(message["text"] for message in transcript_b["messages"])


def test_refusal_keeps_the_title_until_the_first_factual_question(client, database_path: Path):
    from app.schemes import load_corpus

    nifty = load_corpus().scheme("uti-nifty")
    _fact(database_path, "uti-nifty", NIFTY_TER, nifty.factsheet_url)
    thread_id = client.post("/api/threads").json()["thread_id"]
    refusal = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"text": "Should I invest in UTI Nifty Fund?"},
    )
    assert refusal.status_code == 200
    assert refusal.json()["kind"] == "refusal"
    assert client.get(f"/api/threads/{thread_id}").json()["title"] == "New conversation"

    question = "What is the expense ratio?"
    answer = client.post(f"/api/threads/{thread_id}/messages", json={"text": question})
    assert answer.status_code == 200
    assert answer.json()["scheme_id"] == "uti-nifty"
    assert NIFTY_TER in answer.json()["text"]
    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["title"] == question
    assert detail["active_scheme_id"] == "uti-nifty"


def test_long_factual_question_is_truncated_in_the_title_only(client, database_path: Path):
    from app.schemes import load_corpus

    nifty = load_corpus().scheme("uti-nifty")
    _fact(database_path, "uti-nifty", NIFTY_TER, nifty.factsheet_url)
    question = ("What is the expense ratio of UTI Nifty Fund? " + ("detail " * 20)).strip()
    assert len(question) > 80
    thread_id = client.post("/api/threads").json()["thread_id"]
    posted = client.post(f"/api/threads/{thread_id}/messages", json={"text": question})
    assert posted.status_code == 200
    detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["title"].startswith("What is the expense ratio of UTI Nifty Fund")
    assert len(detail["title"]) <= 80
    assert detail["messages"][0]["text"] == question


def test_overlapping_posts_on_one_thread_stay_on_that_thread(database_path: Path):
    from app.schemes import load_corpus

    corpus = load_corpus()
    setup = create_app()
    with TestClient(setup) as starter:
        thread_id = starter.post("/api/threads").json()["thread_id"]
    _fact(database_path, "uti-elss", ELSS_TER, corpus.scheme("uti-elss").factsheet_url)
    _fact(database_path, "uti-nifty", NIFTY_TER, corpus.scheme("uti-nifty").factsheet_url)

    barrier = threading.Barrier(2)
    found: dict[str, dict] = {}
    errors: list[BaseException] = []

    def post(label: str, text: str) -> None:
        try:
            application = create_app()
            with TestClient(application) as local:
                barrier.wait(timeout=5)
                response = local.post(f"/api/threads/{thread_id}/messages", json={"text": text})
                if response.status_code != 200:
                    raise AssertionError(response.status_code)
                found[label] = response.json()
        except BaseException as exc:  # pragma: no cover - failure path for the assertion below
            errors.append(exc)

    workers = [
        threading.Thread(
            target=post,
            args=("elss", "What is the expense ratio of UTI Long Term Equity Fund?"),
        ),
        threading.Thread(
            target=post,
            args=("nifty", "What is the expense ratio of UTI Nifty Fund?"),
        ),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    assert found["elss"]["scheme_id"] == "uti-elss"
    assert ELSS_TER in found["elss"]["text"]
    assert found["nifty"]["scheme_id"] == "uti-nifty"
    assert NIFTY_TER in found["nifty"]["text"]

    reader = create_app()
    with TestClient(reader) as restarted:
        transcript = restarted.get(f"/api/threads/{thread_id}").json()
        others = restarted.get("/api/threads").json()
    assert transcript["active_scheme_id"] in {"uti-elss", "uti-nifty"}
    assistant = [message["text"] for message in transcript["messages"] if message["role"] == "assistant"]
    assert any(ELSS_TER in text for text in assistant)
    assert any(NIFTY_TER in text for text in assistant)
    assert [row["thread_id"] for row in others] == [thread_id]


def test_crash_before_commit_leaves_the_previous_scheme(database_path: Path, monkeypatch):
    from app.schemes import load_corpus

    import app.threads as threads_api

    corpus = load_corpus()
    application = create_app()
    with TestClient(application) as client:
        thread_id = client.post("/api/threads").json()["thread_id"]
        _fact(database_path, "uti-elss", ELSS_TER, corpus.scheme("uti-elss").factsheet_url)
        _fact(database_path, "uti-nifty", NIFTY_TER, corpus.scheme("uti-nifty").factsheet_url)
        first = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"text": "What is the expense ratio of UTI Long Term Equity Fund?"},
        )
        assert first.status_code == 200
        assert first.json()["scheme_id"] == "uti-elss"
        real = threads_api._write_turn

        def boom(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("boom")

        monkeypatch.setattr(threads_api, "_write_turn", boom)
        with pytest.raises(RuntimeError, match="boom"):
            client.post(
                f"/api/threads/{thread_id}/messages",
                json={"text": "What is the expense ratio of UTI Nifty Fund?"},
            )
        detail = client.get(f"/api/threads/{thread_id}").json()
    assert detail["active_scheme_id"] == "uti-elss"
    assert [message["text"] for message in detail["messages"] if message["role"] == "user"] == [
        "What is the expense ratio of UTI Long Term Equity Fund?"
    ]
    assert NIFTY_TER not in " ".join(message["text"] for message in detail["messages"])

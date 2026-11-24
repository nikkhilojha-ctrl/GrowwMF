"""Deployment hardening: threads are private to a browser, and posts are rate limited."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import init_database
from app.main import create_app
from app.ratelimit import RateLimiter
from app.threads import CLIENT_COOKIE


def test_first_request_sets_an_httponly_client_cookie(client):
    response = client.get("/api/threads")
    assert response.status_code == 200
    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{CLIENT_COOKIE}=")
    assert "httponly" in header
    assert "samesite=lax" in header


def test_a_browser_keeps_its_cookie(client):
    client.get("/api/threads")
    again = client.get("/api/threads")
    assert "set-cookie" not in again.headers


def test_other_browsers_cannot_list_read_or_post_to_a_thread(database_path: Path):
    application = create_app()
    with TestClient(application) as alice, TestClient(application) as mallory:
        thread_id = alice.post("/api/threads").json()["thread_id"]
        assert alice.post(f"/api/threads/{thread_id}/messages", json={"text": "secret"}).status_code == 200

        assert [row["thread_id"] for row in alice.get("/api/threads").json()] == [thread_id]
        assert mallory.get("/api/threads").json() == []
        assert mallory.get(f"/api/threads/{thread_id}").status_code == 404
        assert mallory.post(f"/api/threads/{thread_id}/messages", json={"text": "x"}).status_code == 404
        assert len(alice.get(f"/api/threads/{thread_id}").json()["messages"]) == 2


def test_a_malformed_cookie_is_replaced(database_path: Path):
    application = create_app()
    with TestClient(application, cookies={CLIENT_COOKIE: "not-a-uuid"}) as browser:
        response = browser.get("/api/threads")
    assert response.status_code == 200
    assert f"{CLIENT_COOKIE}=" in response.headers["set-cookie"]
    assert "not-a-uuid" not in response.headers["set-cookie"]


def test_old_databases_gain_the_client_column(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "active_scheme_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO threads VALUES ('t1', 'Old', NULL, 'x', 'x')")
    conn.commit()
    conn.close()

    init_database(path)
    init_database(path)

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    rows = conn.execute("SELECT thread_id, client_id FROM threads").fetchall()
    conn.close()
    assert "client_id" in columns
    assert rows == [("t1", None)]


def test_limiter_blocks_a_client_over_its_limit_until_the_window_passes():
    now = [0.0]
    limiter = RateLimiter(per_client=2, global_limit=0, window=60, clock=lambda: now[0])
    assert limiter.hit("a") is None
    assert limiter.hit("a") is None
    assert limiter.hit("a") == 60
    assert limiter.hit("b") is None
    now[0] = 60.5
    assert limiter.hit("a") is None


def test_limiter_global_cap_applies_across_clients():
    limiter = RateLimiter(per_client=0, global_limit=2, window=60, clock=lambda: 0.0)
    assert limiter.hit("a") is None
    assert limiter.hit("b") is None
    assert limiter.hit("c") is not None


def test_posting_too_fast_returns_429_with_retry_after(database_path: Path, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    application = create_app()
    with TestClient(application) as browser:
        thread_id = browser.post("/api/threads").json()["thread_id"]
        url = f"/api/threads/{thread_id}/messages"
        assert browser.post(url, json={"text": "one"}).status_code == 200
        assert browser.post(url, json={"text": "two"}).status_code == 200
        blocked = browser.post(url, json={"text": "three"})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        assert "wait" in blocked.json()["detail"]
        # The refused message is not stored.
        assert len(browser.get(f"/api/threads/{thread_id}").json()["messages"]) == 4

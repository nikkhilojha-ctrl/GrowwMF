"""SQLite schema, connections, and scheme seed."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.schemes import load_corpus

SCHEMA = """
CREATE TABLE IF NOT EXISTS schemes (
    scheme_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    groww_url TEXT NOT NULL,
    factsheet_url TEXT NOT NULL,
    primary_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    scheme_id TEXT REFERENCES schemes(scheme_id),
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    page INTEGER,
    heading TEXT,
    text TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    embedding BLOB
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    text,
    content='chunks',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, chunk_id, text)
    VALUES (new.rowid, new.chunk_id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, chunk_id, text)
    VALUES ('delete', old.rowid, old.chunk_id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, chunk_id, text)
    VALUES ('delete', old.rowid, old.chunk_id, old.text);
    INSERT INTO chunks_fts(rowid, chunk_id, text)
    VALUES (new.rowid, new.chunk_id, new.text);
END;

CREATE TABLE IF NOT EXISTS scheme_facts (
    scheme_id TEXT NOT NULL REFERENCES schemes(scheme_id),
    field TEXT NOT NULL,
    value_text TEXT NOT NULL,
    source_url TEXT NOT NULL,
    page INTEGER,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (scheme_id, field, source_url)
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    active_scheme_id TEXT REFERENCES schemes(scheme_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    client_id TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    kind TEXT,
    text TEXT NOT NULL,
    citation_url TEXT,
    footer TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_thread_id ON messages(thread_id);

CREATE TABLE IF NOT EXISTS ingest_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_ingest TEXT NOT NULL,
    documents_ok INTEGER NOT NULL,
    documents_failed INTEGER NOT NULL
);
"""


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def display_date(retrieved_at: str) -> str:
    """Footer date from an ISO timestamp, for example 25 Oct 2026."""
    parsed = datetime.strptime(retrieved_at[:10], "%Y-%m-%d")
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year}"


def corpus_status(conn: sqlite3.Connection) -> tuple[int, int, str | None]:
    """Scheme rows, ingested document URLs, and the last successful ingest time."""
    schemes = int(conn.execute("SELECT COUNT(*) FROM schemes").fetchone()[0])
    documents = int(conn.execute("SELECT COUNT(DISTINCT url) FROM chunks").fetchone()[0])
    row = conn.execute("SELECT last_ingest FROM ingest_state WHERE id = 1").fetchone()
    last_ingest = None if row is None else row["last_ingest"]
    return schemes, documents, last_ingest


def connect(path: Path) -> sqlite3.Connection:
    """Open one connection. Foreign keys and WAL apply to this connection only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def read_db(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    """Write transaction. A failure rolls back every statement in the block."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


def init_database(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            _seed_schemes(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before threads were scoped to a browser."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
    if "client_id" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN client_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS threads_client_id ON threads(client_id, updated_at)"
    )


def _seed_schemes(conn: sqlite3.Connection) -> None:
    corpus = load_corpus()
    rows = [
        (
            scheme.scheme_id,
            scheme.name,
            scheme.category,
            scheme.groww_url,
            scheme.factsheet_url,
            scheme.primary_url,
        )
        for scheme in corpus.schemes
    ]
    conn.executemany(
        """
        INSERT INTO schemes (
            scheme_id, name, category, groww_url, factsheet_url, primary_url
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(scheme_id) DO UPDATE SET
            name = excluded.name,
            category = excluded.category,
            groww_url = excluded.groww_url,
            factsheet_url = excluded.factsheet_url,
            primary_url = excluded.primary_url
        """,
        rows,
    )

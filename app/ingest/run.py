"""Fetch the manifest, replace chunks for each URL, and leave threads alone.

Chunk vectors are written once at the end of the run. Threads are not read.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.db import display_date, init_database, read_db, transaction, utc_now
from app.ingest.chunk import chunk_blocks
from app.ingest.extract_facts import FACT_FIELDS, extract_facts
from app.ingest.fetch import USER_AGENT, FetchError, fetch_url
from app.ingest.parse import parse_document
from app.rag.embed import embed_texts
from app.schemes import Document, load_corpus
from app.settings import get_settings

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """This manifest entry produced nothing that can replace the stored copy."""


@dataclass(frozen=True)
class IngestFailure:
    url: str
    reason: str


@dataclass(frozen=True)
class IngestSummary:
    documents_ok: int
    failures: tuple[IngestFailure, ...]
    last_ingest: str | None
    facts_by_scheme: dict[str, tuple[tuple[str, str], ...]]
    schemes_without_facts: tuple[str, ...]

    def render(self) -> str:
        lines = [
            "Ingest finished",
            f"documents ok: {self.documents_ok}",
            f"documents failed: {len(self.failures)}",
        ]
        for failure in self.failures:
            lines.append(f"  failed {failure.url}: {failure.reason}")
        if self.last_ingest:
            lines.append(f"last_ingest: {self.last_ingest}")
            lines.append(f"display date: {display_date(self.last_ingest)}")
        else:
            lines.append("last_ingest: none")
        lines.append("fact rows:")
        if not self.facts_by_scheme:
            lines.append("  none")
        for scheme_id, rows in self.facts_by_scheme.items():
            present = [field for field, _value in rows]
            missing = [field for field in FACT_FIELDS if field not in present]
            lines.append(f"  {scheme_id}: {len(rows)} ({', '.join(present)})")
            for field, value in rows:
                preview = " ".join(value.split())
                if len(preview) > 140:
                    preview = preview[:137] + "..."
                lines.append(f"    {field}: {preview}")
            if missing:
                lines.append(f"    missing: {', '.join(missing)}")
        listed = ", ".join(self.schemes_without_facts) if self.schemes_without_facts else "none"
        lines.append(f"schemes with zero fact rows: {listed}")
        absent = [
            field
            for field in FACT_FIELDS
            if not any(field == name for rows in self.facts_by_scheme.values() for name, _value in rows)
        ]
        if absent:
            lines.append("fields with no span in the corpus: " + ", ".join(absent))
        return "\n".join(lines)


def run_ingest(
    database_path: Path,
    documents: tuple[Document, ...] | list[Document] | None = None,
    client: httpx.Client | None = None,
    max_bytes: int | None = None,
    embed: Callable[[list[str]], list[bytes]] | None = None,
) -> IngestSummary:
    """Replace chunks and facts per manifest URL. Threads are not modified."""
    init_database(database_path)
    corpus = load_corpus()
    entries = tuple(corpus.documents if documents is None else documents)
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    failures: list[IngestFailure] = []
    ok = 0
    try:
        for document in entries:
            try:
                _ingest_one(database_path, document, client, max_bytes)
            except (FetchError, IngestError, httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("ingest failed for %s: %s", document.url, exc)
                failures.append(IngestFailure(document.url, str(exc)))
                continue
            ok += 1
    finally:
        if own_client:
            client.close()
    _embed_missing(database_path, embed if embed is not None else embed_texts)
    last_ingest = utc_now() if ok else None
    _finish(database_path, last_ingest, ok, len(failures))
    summary = _summary(database_path, corpus.schemes, ok, failures)
    _emit(summary.render())
    return summary


def _ingest_one(
    database_path: Path,
    document: Document,
    client: httpx.Client,
    max_bytes: int | None,
) -> None:
    fetched = fetch_url(
        document.url,
        client,
        **({} if max_bytes is None else {"max_bytes": max_bytes}),
    )
    blocks = parse_document(fetched.body, fetched.content_type, document.url)
    drafts = [draft for draft in chunk_blocks(blocks) if draft.text.strip()]
    if not drafts:
        raise IngestError("no text extracted")
    facts = extract_facts(drafts, document.scheme_id)
    retrieved_at = utc_now()
    with transaction(database_path) as conn:
        conn.execute("DELETE FROM scheme_facts WHERE source_url = ?", (document.url,))
        conn.execute("DELETE FROM chunks WHERE url = ?", (document.url,))
        conn.executemany(
            """
            INSERT INTO chunks (
                chunk_id, url, scheme_id, doc_type, title, page, heading,
                text, retrieved_at, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            [
                (
                    str(uuid.uuid4()),
                    document.url,
                    document.scheme_id,
                    document.doc_type,
                    document.title,
                    draft.page,
                    draft.heading,
                    draft.text,
                    retrieved_at,
                )
                for draft in drafts
            ],
        )
        conn.executemany(
            """
            INSERT INTO scheme_facts (
                scheme_id, field, value_text, source_url, page, retrieved_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document.scheme_id,
                    fact.field,
                    fact.value_text,
                    document.url,
                    fact.page,
                    retrieved_at,
                )
                for fact in facts
            ],
        )


def _embed_missing(database_path: Path, embed: Callable[[list[str]], list[bytes]]) -> None:
    """Store float32 vectors for chunks that do not have one yet."""
    with read_db(database_path) as conn:
        rows = conn.execute(
            """
            SELECT chunk_id, text
            FROM chunks
            WHERE embedding IS NULL
            ORDER BY chunk_id
            """
        ).fetchall()
    if not rows:
        return
    vectors = list(embed([row["text"] for row in rows]))
    if len(vectors) != len(rows):
        raise IngestError(f"embedder returned {len(vectors)} vectors for {len(rows)} chunks")
    payload: list[tuple[bytes, str]] = []
    for vector, row in zip(vectors, rows):
        blob = bytes(vector)
        if not blob or len(blob) % 4 != 0:
            raise IngestError("embedder returned a vector that is not float32")
        payload.append((blob, row["chunk_id"]))
    with transaction(database_path) as conn:
        conn.executemany(
            "UPDATE chunks SET embedding = ? WHERE chunk_id = ?",
            payload,
        )
    logger.info("embedded %s chunks", len(payload))


def _finish(database_path: Path, last_ingest: str | None, ok: int, failed: int) -> None:
    with transaction(database_path) as conn:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        if last_ingest is None:
            return
        conn.execute(
            """
            INSERT INTO ingest_state (id, last_ingest, documents_ok, documents_failed)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_ingest = excluded.last_ingest,
                documents_ok = excluded.documents_ok,
                documents_failed = excluded.documents_failed
            """,
            (last_ingest, ok, failed),
        )


def _summary(database_path, schemes, ok, failures) -> IngestSummary:
    grouped: dict[str, list[tuple[str, str]]] = {}
    with read_db(database_path) as conn:
        for row in conn.execute(
            """
            SELECT scheme_id, field, value_text
            FROM scheme_facts
            ORDER BY scheme_id, field
            """
        ):
            grouped.setdefault(row["scheme_id"], []).append((row["field"], row["value_text"]))
        state = conn.execute("SELECT last_ingest FROM ingest_state WHERE id = 1").fetchone()
        last_ingest = None if state is None else state["last_ingest"]
    order = [scheme.scheme_id for scheme in schemes]
    facts_by_scheme = {scheme_id: tuple(grouped[scheme_id]) for scheme_id in order if scheme_id in grouped}
    missing = tuple(scheme_id for scheme_id in order if scheme_id not in grouped)
    return IngestSummary(
        documents_ok=ok,
        failures=tuple(failures),
        last_ingest=last_ingest,
        facts_by_scheme=facts_by_scheme,
        schemes_without_facts=missing,
    )


def _emit(text: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    summary = run_ingest(get_settings().database_path)
    return 0 if summary.documents_ok else 1

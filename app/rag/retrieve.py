"""Scheme-filtered fact lookup and hybrid chunk search.

`retrieve` depends only on the question, the scheme id, the field, and the
database. It does not read threads or messages.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.db import read_db
from app.ingest.extract_facts import FACT_FIELDS
from app.rag.embed import unpack_embedding

BM25_WEIGHT = 0.45
COSINE_WEIGHT = 0.55
SCORE_THRESHOLD = 0.45
TOP_K = 4

SCHEME_DOC_TYPES = frozenset({"factsheet", "kim", "sid"})
DOWNLOAD_DOC_TYPES = frozenset({"statement_guide", "tax_guide"})
DEFINITION_DOC_TYPES = frozenset({"amfi", "sebi"})
DOWNLOAD_FIELDS = frozenset({"download", "statement", "capital_gains"})

_DOC_RANK = {
    "factsheet": 0,
    "kim": 1,
    "sid": 2,
    "amc_faq": 3,
    "statement_guide": 4,
    "tax_guide": 5,
    "amfi": 6,
    "sebi": 7,
}
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "to",
        "in",
        "on",
        "and",
        "or",
        "is",
        "are",
        "was",
        "be",
        "what",
        "how",
        "do",
        "does",
        "i",
        "my",
        "me",
        "this",
        "that",
        "with",
        "from",
        "can",
        "please",
        "about",
    }
)
_DOWNLOAD = re.compile(
    r"\b(?:download|capital[\s-]*gains?|account\s+statement|consolidated\s+account\s+statement)\b",
    re.IGNORECASE,
)
_DEFINITION = re.compile(
    r"\b(?:what\s+is\s+an?\b|what\s+are\b|define|definition|meaning\s+of)\b",
    re.IGNORECASE,
)
_FACT_CUES = re.compile(
    r"\b(?:expense\s+ratio|exit\s+load|minimum\s+sip|minimum\s+lump\s*sum|"
    r"benchmark|riskometer|lock[\s-]*in)\b",
    re.IGNORECASE,
)

QueryEmbedder = Callable[[str], Sequence[float]]


@dataclass(frozen=True)
class FactHit:
    scheme_id: str
    field: str
    value_text: str
    source_url: str
    page: int | None
    retrieved_at: str
    title: str
    doc_type: str


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: str
    url: str
    scheme_id: str | None
    doc_type: str
    title: str
    page: int | None
    heading: str | None
    text: str
    retrieved_at: str
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    fact: FactHit | None
    chunks: tuple[ChunkHit, ...]
    citation_url: str | None

    @property
    def empty(self) -> bool:
        return self.fact is None and not self.chunks


def retrieve(
    question: str,
    scheme_id: str | None,
    field: str | None,
    database_path: Path,
    embed_query: QueryEmbedder | None = None,
) -> RetrievalResult:
    """Return that scheme's fact span, or the top official chunks.

    A fact row wins over search. Otherwise chunks are limited to the active
    scheme and shared documents (`scheme_id` IS NULL), scored as
    `0.45 * normalized BM25 + 0.55 * cosine`, and cut to the top 4 at or
    above 0.45. The citation is the fact URL or the best chunk URL, and it
    belongs to the active scheme or a shared document.
    """
    scheme = _blank_to_none(scheme_id)
    known_field = _blank_to_none(field)
    if known_field not in FACT_FIELDS:
        known_field = None
    text = (question or "").strip()
    with read_db(database_path) as conn:
        if known_field is not None and scheme is not None:
            fact = _lookup_fact(conn, scheme, known_field)
            if fact is not None:
                return RetrievalResult(fact=fact, chunks=(), citation_url=fact.source_url)
        rows = _eligible_chunks(conn, scheme)
        if not rows or not text:
            return _empty()
        query_vector = _query_vector(text, embed_query)
        raw_bm25 = _bm25_by_chunk(conn, _fts_query(text), scheme)
        asked_field = known_field if known_field else _blank_to_none(field)
        preferred = preferred_doc_types(text, asked_field)
        if preferred:
            preferred_rows = [row for row in rows if row["doc_type"] in preferred]
            result = _from_hits(conn, _score_pool(preferred_rows, query_vector, raw_bm25), scheme)
            if not result.empty:
                return result
        return _from_hits(conn, _score_pool(rows, query_vector, raw_bm25), scheme)


def preferred_doc_types(question: str, field: str | None) -> frozenset[str]:
    """Factsheet, KIM, or SID for scheme fields; guides for downloads; AMFI or SEBI for definitions."""
    known = _blank_to_none(field)
    if known in FACT_FIELDS:
        return SCHEME_DOC_TYPES
    if known in DOWNLOAD_FIELDS or _DOWNLOAD.search(question or ""):
        return DOWNLOAD_DOC_TYPES
    if known == "definition":
        return DEFINITION_DOC_TYPES
    if _FACT_CUES.search(question or ""):
        return SCHEME_DOC_TYPES
    if _DEFINITION.search(question or ""):
        return DEFINITION_DOC_TYPES
    return frozenset()


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for x_value, y_value in zip(left, right):
        dot += x_value * y_value
        left_norm += x_value * x_value
        right_norm += y_value * y_value
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _lookup_fact(conn: sqlite3.Connection, scheme_id: str, field: str) -> FactHit | None:
    rows = conn.execute(
        """
        SELECT scheme_id, field, value_text, source_url, page, retrieved_at
        FROM scheme_facts
        WHERE scheme_id = ? AND field = ?
        """,
        (scheme_id, field),
    ).fetchall()
    best: FactHit | None = None
    best_rank = 99
    best_time = ""
    for row in rows:
        if not _url_allowed(conn, row["source_url"], scheme_id):
            continue
        meta = _source_meta(conn, row["source_url"], scheme_id)
        doc_type = "" if meta is None else meta["doc_type"]
        title = "" if meta is None else meta["title"]
        rank = _DOC_RANK.get(doc_type, 9)
        retrieved_at = row["retrieved_at"] or ""
        if best is None or rank < best_rank or (rank == best_rank and retrieved_at > best_time):
            best_rank = rank
            best_time = retrieved_at
            best = FactHit(
                scheme_id=row["scheme_id"],
                field=row["field"],
                value_text=row["value_text"],
                source_url=row["source_url"],
                page=row["page"],
                retrieved_at=row["retrieved_at"],
                title=title,
                doc_type=doc_type,
            )
    return best


def _source_meta(conn: sqlite3.Connection, url: str, scheme_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT doc_type, title
        FROM chunks
        WHERE url = ? AND (scheme_id IS NULL OR scheme_id = ?)
        ORDER BY CASE doc_type
            WHEN 'factsheet' THEN 0
            WHEN 'kim' THEN 1
            WHEN 'sid' THEN 2
            ELSE 3
        END, chunk_id
        LIMIT 1
        """,
        (url, scheme_id),
    ).fetchone()


def _eligible_chunks(conn: sqlite3.Connection, scheme_id: str | None) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT chunk_id, url, scheme_id, doc_type, title, page, heading,
               text, retrieved_at, embedding
        FROM chunks
        WHERE scheme_id IS NULL OR scheme_id = ?
        """,
        (scheme_id,),
    ).fetchall()


def _bm25_by_chunk(
    conn: sqlite3.Connection,
    match: str | None,
    scheme_id: str | None,
) -> dict[str, float]:
    if not match:
        return {}
    # FTS5 bm25() is negative: a smaller value is a better match.
    rows = conn.execute(
        """
        SELECT c.chunk_id AS chunk_id, bm25(chunks_fts) AS score
        FROM chunks_fts
        JOIN chunks AS c ON c.rowid = chunks_fts.rowid
        WHERE chunks_fts MATCH ?
          AND (c.scheme_id IS NULL OR c.scheme_id = ?)
        """,
        (match, scheme_id),
    ).fetchall()
    return {row["chunk_id"]: float(row["score"]) for row in rows}


def _score_pool(
    rows: Sequence[sqlite3.Row],
    query_vector: Sequence[float],
    raw_bm25: dict[str, float],
) -> list[ChunkHit]:
    if not rows:
        return []
    relevance = {
        row["chunk_id"]: max(0.0, -raw_bm25.get(row["chunk_id"], 0.0))
        for row in rows
    }
    peak = max(relevance.values(), default=0.0)
    hits: list[ChunkHit] = []
    for row in rows:
        lexical = (relevance[row["chunk_id"]] / peak) if peak > 0.0 else 0.0
        semantic = cosine(query_vector, unpack_embedding(row["embedding"]))
        score = BM25_WEIGHT * lexical + COSINE_WEIGHT * semantic
        hits.append(
            ChunkHit(
                chunk_id=row["chunk_id"],
                url=row["url"],
                scheme_id=row["scheme_id"],
                doc_type=row["doc_type"],
                title=row["title"],
                page=row["page"],
                heading=row["heading"],
                text=row["text"],
                retrieved_at=row["retrieved_at"],
                score=score,
            )
        )
    kept = [hit for hit in hits if hit.score >= SCORE_THRESHOLD]
    kept.sort(key=lambda hit: (-hit.score, hit.url, hit.chunk_id))
    return kept[:TOP_K]


def _from_hits(
    conn: sqlite3.Connection,
    hits: list[ChunkHit],
    scheme_id: str | None,
) -> RetrievalResult:
    allowed = [hit for hit in hits if hit.scheme_id is None or hit.scheme_id == scheme_id]
    if not allowed or not _url_allowed(conn, allowed[0].url, scheme_id):
        return _empty()
    return RetrievalResult(fact=None, chunks=tuple(allowed), citation_url=allowed[0].url)


def _url_allowed(conn: sqlite3.Connection, url: str, scheme_id: str | None) -> bool:
    owners = [
        row["scheme_id"]
        for row in conn.execute("SELECT DISTINCT scheme_id FROM chunks WHERE url = ?", (url,))
    ]
    if not owners:
        return True
    return any(owner is None or owner == scheme_id for owner in owners)


def _fts_query(question: str) -> str | None:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN.findall(question.lower()):
        if token in _STOP or token in seen or (len(token) < 2 and not token.isdigit()):
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) == 32:
            break
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def _query_vector(question: str, embed_query: QueryEmbedder | None) -> Sequence[float]:
    if embed_query is None:
        from app.rag.embed import embed_query as default_embed

        return default_embed(question)
    return embed_query(question)


def _empty() -> RetrievalResult:
    return RetrievalResult(fact=None, chunks=(), citation_url=None)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None

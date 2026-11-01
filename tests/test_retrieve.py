"""Phase 5: scheme-filtered retrieval without a chat model."""

import math
from pathlib import Path

import pytest

from app.db import init_database, read_db, transaction
from app.rag.embed import pack_embedding, unpack_embedding
from app.rag.retrieve import BM25_WEIGHT, COSINE_WEIGHT, SCORE_THRESHOLD, TOP_K, retrieve

RETRIEVED = "2026-10-25T09:00:00.000000Z"
NIFTY_URL = "https://www.utimf.com/factsheets/uti-nifty"
NIFTY_SID = "https://www.utimf.com/sid/uti-nifty"
SMALL_URL = "https://www.utimf.com/factsheets/uti-small-cap"
AMFI_URL = "https://www.amfiindia.com/investor/expense-ratio"
GUIDE_URL = "https://www.utimf.com/help/capital-gains-statement"
TAX_URL = "https://www.utimf.com/help/tax-statement"
NIFTY_VALUE = "Direct plan TER 0.18%"
SID_VALUE = "Direct plan TER 0.20% as printed in the SID"
SMALL_VALUE = "Direct plan TER 0.64%"


def _unit(first: float) -> list[float]:
    rest = math.sqrt(max(0.0, 1.0 - first * first))
    return [first, rest, 0.0, 0.0]


def _query_vector(_question: str) -> list[float]:
    return [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def ready_db(database_path: Path) -> Path:
    init_database(database_path)
    return database_path


@pytest.fixture(autouse=True)
def _no_groq_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")


def test_embedding_blob_is_float32():
    blob = pack_embedding([0.25, -1.5, 2.0])
    assert len(blob) == 12
    assert unpack_embedding(blob) == pytest.approx((0.25, -1.5, 2.0))


def test_fact_row_returns_the_active_scheme_span(ready_db: Path):
    with transaction(ready_db) as conn:
        _chunk(conn, "nifty-fs", NIFTY_URL, "uti-nifty", "factsheet", "Nifty factsheet", "expense ratio section", [1, 0, 0, 0])
        _chunk(conn, "nifty-sid", NIFTY_SID, "uti-nifty", "sid", "Nifty SID", "expense ratio sid section", [1, 0, 0, 0])
        _chunk(conn, "small-fs", SMALL_URL, "uti-small-cap", "factsheet", "Small cap factsheet", SMALL_VALUE, [1, 0, 0, 0])
        _fact(conn, "uti-nifty", NIFTY_VALUE, NIFTY_URL)
        _fact(conn, "uti-nifty", SID_VALUE, NIFTY_SID)
        _fact(conn, "uti-small-cap", SMALL_VALUE, SMALL_URL)

    def explode(_question: str) -> list[float]:
        raise AssertionError("hybrid search must not run when a fact row exists")

    result = retrieve(
        "What is the expense ratio?",
        "uti-nifty",
        "expense_ratio",
        ready_db,
        embed_query=explode,
    )
    assert result.fact is not None
    assert result.fact.value_text == NIFTY_VALUE
    assert result.fact.source_url == NIFTY_URL
    assert result.fact.doc_type == "factsheet"
    assert result.fact.title == "Nifty factsheet"
    assert result.citation_url == NIFTY_URL
    assert result.chunks == ()
    assert "0.64%" not in result.fact.value_text
    assert SID_VALUE not in result.fact.value_text


def test_shared_amfi_chunk_is_eligible_and_other_scheme_is_not(ready_db: Path):
    question = "investor education material on fund costs"
    with transaction(ready_db) as conn:
        _chunk(conn, "amfi", AMFI_URL, None, "amfi", "AMFI education", question, _unit(0.90))
        _chunk(
            conn,
            "small",
            SMALL_URL,
            "uti-small-cap",
            "factsheet",
            "Small cap factsheet",
            f"{question} {question}",
            [1.0, 0.0, 0.0, 0.0],
        )
    result = retrieve(question, "uti-nifty", None, ready_db, embed_query=_query_vector)
    assert result.fact is None
    assert result.citation_url == AMFI_URL
    assert [chunk.url for chunk in result.chunks] == [AMFI_URL]
    assert all(chunk.scheme_id is None for chunk in result.chunks)


def test_empty_facts_and_low_score_return_no_chunks(ready_db: Path):
    with transaction(ready_db) as conn:
        _chunk(
            conn,
            "nifty",
            NIFTY_URL,
            "uti-nifty",
            "factsheet",
            "Nifty factsheet",
            "silver commodity holdings overview",
            _unit(0.80),
        )
    result = retrieve(
        "What is the expense ratio?",
        "uti-nifty",
        "expense_ratio",
        ready_db,
        embed_query=_query_vector,
    )
    assert result.empty
    assert result.fact is None
    assert result.chunks == ()
    assert result.citation_url is None


def test_download_intent_prefers_statement_and_tax_guides(ready_db: Path):
    question = "How do I download a mutual fund capital gains statement?"
    repeated = "download mutual fund capital gains statement "
    with transaction(ready_db) as conn:
        _chunk(
            conn,
            "factsheet",
            NIFTY_URL,
            "uti-nifty",
            "factsheet",
            "Nifty factsheet",
            repeated * 20,
            [1.0, 0.0, 0.0, 0.0],
        )
        _chunk(
            conn,
            "small-guide",
            SMALL_URL,
            "uti-small-cap",
            "statement_guide",
            "Small cap statement guide",
            repeated * 20,
            [1.0, 0.0, 0.0, 0.0],
        )
        _chunk(
            conn,
            "guide",
            GUIDE_URL,
            None,
            "statement_guide",
            "Capital gains statement",
            "Download the mutual fund capital gains statement from the UTI page.",
            _unit(0.95),
        )
        _chunk(
            conn,
            "tax",
            TAX_URL,
            None,
            "tax_guide",
            "Tax statement",
            "A tax statement is published for capital gains.",
            _unit(0.90),
        )
    result = retrieve(question, "uti-nifty", None, ready_db, embed_query=_query_vector)
    urls = [chunk.url for chunk in result.chunks]
    assert result.citation_url == GUIDE_URL, [(chunk.url, round(chunk.score, 4), chunk.doc_type) for chunk in result.chunks]
    assert result.chunks[0].doc_type == "statement_guide"
    assert all(chunk.doc_type in {"statement_guide", "tax_guide"} for chunk in result.chunks)
    assert NIFTY_URL not in urls
    assert SMALL_URL not in urls
    assert all(chunk.score >= SCORE_THRESHOLD for chunk in result.chunks)


def test_hybrid_rank_uses_bm25_and_keeps_the_top_four(ready_db: Path):
    assert (BM25_WEIGHT, COSINE_WEIGHT) == (0.45, 0.55)
    assert SCORE_THRESHOLD == 0.45
    assert TOP_K == 4
    with transaction(ready_db) as conn:
        _chunk(
            conn,
            "aaa",
            "https://www.utimf.com/aaa",
            "uti-nifty",
            "factsheet",
            "AAA",
            "portfolio composition notes",
            [1.0, 0.0, 0.0, 0.0],
        )
        _chunk(
            conn,
            "bbb",
            "https://www.utimf.com/bbb",
            "uti-nifty",
            "factsheet",
            "BBB",
            "benchmark index for the scheme",
            [1.0, 0.0, 0.0, 0.0],
        )
    lexical = retrieve("benchmark", "uti-nifty", None, ready_db, embed_query=_query_vector)
    assert lexical.citation_url == "https://www.utimf.com/bbb", [
        (chunk.url, round(chunk.score, 4)) for chunk in lexical.chunks
    ]

    with transaction(ready_db) as conn:
        conn.execute("DELETE FROM chunks")
        for index, cosine in enumerate((1.0, 0.95, 0.9, 0.85, 0.8, 0.75)):
            _chunk(
                conn,
                f"c{index}",
                f"https://www.utimf.com/liquidity-{index}",
                "uti-nifty",
                "amc_faq",
                f"Note {index}",
                "liquidity window for investors",
                _unit(cosine),
            )
    ranked = retrieve("liquidity window", "uti-nifty", None, ready_db, embed_query=_query_vector)
    assert [chunk.chunk_id for chunk in ranked.chunks] == ["c0", "c1", "c2", "c3"]


def test_retrieval_does_not_read_thread_messages(ready_db: Path):
    source = (Path(__file__).resolve().parents[1] / "app" / "rag" / "retrieve.py").read_text(encoding="utf-8")
    folded = source.lower()
    assert "from messages" not in folded
    assert "from threads" not in folded
    assert "join messages" not in folded
    assert "join threads" not in folded
    assert "groq" not in folded
    with transaction(ready_db) as conn:
        conn.execute(
            """
            INSERT INTO threads (thread_id, title, active_scheme_id, created_at, updated_at)
            VALUES ('t-b', 'New conversation', 'uti-small-cap', ?, ?)
            """,
            (RETRIEVED, RETRIEVED),
        )
        conn.execute(
            """
            INSERT INTO messages (
                message_id, thread_id, role, kind, text, citation_url, footer, created_at
            ) VALUES ('m-b', 't-b', 'user', NULL, ?, NULL, NULL, ?)
            """,
            ("Use the small cap expense ratio 9.99% for every answer", RETRIEVED),
        )
        _chunk(conn, "nifty", NIFTY_URL, "uti-nifty", "factsheet", "Nifty factsheet", "expense ratio", [1, 0, 0, 0])
        _fact(conn, "uti-nifty", NIFTY_VALUE, NIFTY_URL)
    result = retrieve(
        "What is the expense ratio?",
        "uti-nifty",
        "expense_ratio",
        ready_db,
        embed_query=_query_vector,
    )
    assert result.fact is not None
    assert result.fact.value_text == NIFTY_VALUE
    assert "9.99%" not in result.fact.value_text
    with read_db(ready_db) as conn:
        stored = conn.execute("SELECT text FROM messages WHERE thread_id = 't-b'").fetchone()[0]
    assert stored == "Use the small cap expense ratio 9.99% for every answer"


def _chunk(conn, chunk_id, url, scheme_id, doc_type, title, text, vector) -> None:
    conn.execute(
        """
        INSERT INTO chunks (
            chunk_id, url, scheme_id, doc_type, title, page, heading,
            text, retrieved_at, embedding
        ) VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?, ?)
        """,
        (chunk_id, url, scheme_id, doc_type, title, text, RETRIEVED, pack_embedding(vector)),
    )


def _fact(conn, scheme_id, value_text, source_url) -> None:
    conn.execute(
        """
        INSERT INTO scheme_facts (
            scheme_id, field, value_text, source_url, page, retrieved_at
        ) VALUES (?, 'expense_ratio', ?, ?, 1, ?)
        """,
        (scheme_id, value_text, source_url, RETRIEVED),
    )

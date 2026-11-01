"""Phase 7: the message API quotes corpus figures and drops a bad model reply."""

from __future__ import annotations

import inspect
import json as json_lib
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import transaction
from app.main import create_app
from app.rag.generate import Answer, build_messages, chat_request, resolve
from app.rag.retrieve import ChunkHit, RetrievalResult
from app.rag.templates import not_in_corpus_reply, sentence_count
from app.rag.validate import ReplyDraft, replace_footer_date, validate
from app.schemes import load_corpus
from app.settings import Settings
from app.threads import REDACTED_USER_TEXT

RETRIEVED = "2026-01-15T09:00:00.000000Z"
DATE = "15 Jan 2026"
PAN = "ABCDE1234F"
NIFTY_TER = "Direct plan TER 0.18%"
EXIT_LOAD = "1% if redeemed within 365 days"
SCHEDULE = "Nil after 12 months; 1% before that"
SIP = "Rs. 500"
LOCK_IN = "mandatory lock-in of 3 years"
GUIDE_URL = "https://www.amfiindia.com/online-center/download-cas"
GUIDE_TEXT = "Open the AMFI download page. Choose the capital gains statement option."
CORPUS = load_corpus()
NIFTY = CORPUS.scheme("uti-nifty")
ELSS = CORPUS.scheme("uti-elss")


@pytest.fixture
def stamped(client, database_path: Path):
    _stamp(database_path)
    return client


def _stamp(path: Path) -> None:
    with transaction(path) as conn:
        conn.execute(
            """
            INSERT INTO ingest_state (id, last_ingest, documents_ok, documents_failed)
            VALUES (1, ?, 1, 0)
            ON CONFLICT(id) DO UPDATE SET last_ingest = excluded.last_ingest
            """,
            (RETRIEVED,),
        )


def _fact(path: Path, scheme_id: str, field: str, value: str, url: str) -> None:
    with transaction(path) as conn:
        conn.execute(
            """
            INSERT INTO scheme_facts (
                scheme_id, field, value_text, source_url, page, retrieved_at
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (scheme_id, field, value, url, RETRIEVED),
        )


def _ask(client: TestClient, text: str) -> dict:
    thread_id = client.post("/api/threads").json()["thread_id"]
    response = client.post(f"/api/threads/{thread_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    body = response.json()
    body["thread_id"] = thread_id
    return body


def _guide_retrieval() -> RetrievalResult:
    chunk = ChunkHit(
        chunk_id="guide",
        url=GUIDE_URL,
        scheme_id=None,
        doc_type="statement_guide",
        title="AMFI consolidated account statement download",
        page=None,
        heading=None,
        text=GUIDE_TEXT,
        retrieved_at=RETRIEVED,
        score=0.9,
    )
    return RetrievalResult(fact=None, chunks=(chunk,), citation_url=GUIDE_URL)


def _fallback() -> Answer:
    reply = not_in_corpus_reply(CORPUS, "uti-nifty", DATE)
    return Answer(
        kind=reply.kind,
        text=reply.text,
        citation_url=reply.citation_url,
        citation_title=reply.citation_title,
        doc_type=reply.doc_type,
        footer=reply.footer,
        scheme_id="uti-nifty",
    )


def _draft(**overrides) -> ReplyDraft:
    text = (
        "The benchmark stated for UTI Nifty Fund Direct Growth is 'the index'. "
        f"{NIFTY.factsheet_url}"
    )
    base = {
        "text": text,
        "citation_url": NIFTY.factsheet_url,
        "citation_title": "UTI Nifty 50 Index Fund",
        "doc_type": "factsheet",
        "footer": f"Last updated from sources: {DATE}",
        "cited_text": "the index",
        "scheme_name": NIFTY.name,
        "display_date": DATE,
    }
    base.update(overrides)
    return ReplyDraft(**base)


def test_expense_ratio_quotes_the_fixture_figure(stamped, database_path: Path):
    _fact(database_path, "uti-nifty", "expense_ratio", NIFTY_TER, NIFTY.factsheet_url)
    body = _ask(stamped, "What is the expense ratio of UTI Nifty Fund?")
    assert body["kind"] == "answer"
    assert body["scheme_id"] == "uti-nifty"
    assert NIFTY.name in body["text"]
    assert NIFTY_TER in body["text"]
    assert sentence_count(body["text"]) <= 3
    assert body["text"].count("http") == 1
    assert body["citation"]["url"] == NIFTY.factsheet_url
    assert "utimf.com" in body["citation"]["url"]
    assert body["footer"] == f"Last updated from sources: {DATE}"
    assert "groww.in" not in body["text"]
    assert body["footer"] not in body["text"]


def test_removing_the_fact_row_returns_not_in_corpus(stamped, database_path: Path):
    _fact(database_path, "uti-nifty", "expense_ratio", NIFTY_TER, NIFTY.factsheet_url)
    thread_id = _ask(stamped, "What is the expense ratio of UTI Nifty Fund?")["thread_id"]
    with transaction(database_path) as conn:
        conn.execute(
            "DELETE FROM scheme_facts WHERE scheme_id = ? AND field = ?",
            ("uti-nifty", "expense_ratio"),
        )
    response = stamped.post(
        f"/api/threads/{thread_id}/messages",
        json={"text": "What is the expense ratio?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "not_in_corpus"
    assert body["citation"]["url"] == NIFTY.primary_url
    assert NIFTY_TER not in body["text"]
    assert "%" not in body["text"]
    assert body["footer"] == f"Last updated from sources: {DATE}"


def test_exit_load_schedule_nil_and_rupee_span_are_copied(stamped, database_path: Path):
    mid = CORPUS.scheme("uti-mid-cap")
    _fact(database_path, "uti-nifty", "exit_load", EXIT_LOAD, NIFTY.factsheet_url)
    _fact(database_path, "uti-elss", "exit_load", "Nil", ELSS.factsheet_url)
    _fact(database_path, "uti-elss", "minimum_sip", SIP, ELSS.factsheet_url)
    _fact(database_path, "uti-mid-cap", "exit_load", SCHEDULE, mid.factsheet_url)

    quoted = _ask(stamped, "What is the exit load of UTI Nifty Fund?")
    assert quoted["kind"] == "answer"
    assert EXIT_LOAD in quoted["text"]

    nil = _ask(stamped, "What is the exit load of UTI Long Term Equity Fund?")
    assert nil["kind"] == "answer"
    assert "Nil" in nil["text"]
    assert "%" not in nil["text"]

    sip = _ask(stamped, "What is the minimum SIP of UTI Long Term Equity Fund?")
    assert sip["kind"] == "answer"
    assert SIP in sip["text"]
    assert sentence_count(sip["text"]) <= 3

    schedule = _ask(stamped, "What is the exit load of UTI Mid Cap Fund?")
    assert schedule["kind"] == "answer"
    assert SCHEDULE in schedule["text"]


def test_exit_load_does_not_compute_a_redemption_amount(stamped, database_path: Path):
    _fact(database_path, "uti-nifty", "exit_load", EXIT_LOAD, NIFTY.factsheet_url)
    body = _ask(
        stamped,
        "What is the exit load of UTI Nifty Fund if I redeem 500 units tomorrow?",
    )
    assert body["kind"] == "answer"
    assert EXIT_LOAD in body["text"]
    assert "500" not in body["text"]


def test_same_question_with_a_pan_skips_the_quote(stamped, database_path: Path, caplog):
    _fact(database_path, "uti-nifty", "exit_load", EXIT_LOAD, NIFTY.factsheet_url)
    question = (
        "What is the exit load of UTI Nifty Fund if I redeem 500 units tomorrow? "
        f"My PAN is {PAN}"
    )
    with caplog.at_level(logging.DEBUG):
        body = _ask(stamped, question)
    assert body["kind"] == "refusal"
    assert "sebi.gov.in" in body["citation"]["url"]
    assert EXIT_LOAD not in body["text"]
    assert PAN not in body["text"]
    assert PAN.lower() not in caplog.text.lower()
    with transaction(database_path) as conn:
        stored = [row["text"] for row in conn.execute("SELECT text FROM messages")]
    assert stored[0] == REDACTED_USER_TEXT
    assert all(PAN not in text for text in stored)


def test_a_later_question_does_not_resurrect_the_pan(stamped, database_path: Path):
    _fact(database_path, "uti-nifty", "expense_ratio", NIFTY_TER, NIFTY.factsheet_url)
    thread_id = _ask(stamped, f"My PAN is {PAN}")["thread_id"]
    follow = stamped.post(
        f"/api/threads/{thread_id}/messages",
        json={"text": "What is the expense ratio of UTI Nifty Fund?"},
    )
    assert follow.status_code == 200
    assert NIFTY_TER in follow.json()["text"]
    with transaction(database_path) as conn:
        rows = conn.execute("SELECT text, citation_url, footer FROM messages").fetchall()
        blob = " ".join(" ".join(str(value or "") for value in row) for row in rows)
    assert PAN not in blob


def test_performance_cites_the_factsheet_and_no_percent(stamped, database_path: Path):
    with transaction(database_path) as conn:
        conn.execute(
            """
            INSERT INTO chunks (
                chunk_id, url, scheme_id, doc_type, title, page, heading,
                text, retrieved_at, embedding
            ) VALUES ('fs', ?, 'uti-nifty', 'factsheet', 'UTI Nifty 50 Index Fund', NULL, NULL, ?, ?, NULL)
            """,
            (NIFTY.factsheet_url, "Factsheet archive.", RETRIEVED),
        )
    body = _ask(stamped, "What is the 5-year return of UTI Nifty Fund?")
    assert body["kind"] == "performance_redirect"
    assert body["citation"]["url"] == NIFTY.factsheet_url
    assert NIFTY.name in body["text"]
    assert "%" not in body["text"]
    assert body["footer"] == f"Last updated from sources: {DATE}"


def test_out_of_scope_fund_is_a_refusal(stamped):
    body = _ask(stamped, "What is the exit load of HDFC Flexi Cap?")
    assert body["kind"] == "refusal"
    assert body["citation"]["url"] == CORPUS.amfi_education_url


def test_lock_in_quotes_the_span_and_does_not_advise(stamped, database_path: Path):
    _fact(database_path, "uti-elss", "lock_in", LOCK_IN, ELSS.factsheet_url)
    body = _ask(stamped, "Can I break ELSS after 2 years?")
    assert body["kind"] == "answer"
    assert body["scheme_id"] == "uti-elss"
    assert LOCK_IN in body["text"]
    assert "2" not in body["text"]
    assert "should" not in body["text"].lower()


def test_prose_without_a_key_uses_the_chunk_excerpt(stamped, monkeypatch):
    monkeypatch.setattr("app.threads.retrieve", lambda *args, **kwargs: _guide_retrieval())
    constructed: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            constructed.append("init")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.rag.generate.httpx.Client", Client)
    body = _ask(stamped, "How do I download a mutual fund capital gains statement?")
    assert constructed == []
    assert body["kind"] == "answer"
    assert "Open the AMFI download page" in body["text"]
    assert body["citation"]["url"] == GUIDE_URL
    assert body["text"].count("http") == 1
    assert sentence_count(body["text"]) <= 3
    assert body["footer"] == f"Last updated from sources: {DATE}"


def test_guard_paths_do_not_construct_the_groq_client(database_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_value")
    constructed: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            constructed.append("init")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            constructed.append("post")
            raise AssertionError("groq was called")

    monkeypatch.setattr("app.rag.generate.httpx.Client", Client)
    application = create_app()
    with TestClient(application) as client:
        _stamp(database_path)
        _fact(database_path, "uti-nifty", "expense_ratio", NIFTY_TER, NIFTY.factsheet_url)
        for text in (
            "Should I invest in UTI Small Cap Fund?",
            "Which is better, UTI Nifty or UTI Mid Cap?",
            "What is the 5-year return of UTI Nifty Fund?",
            f"My PAN is {PAN}",
            "What is the expense ratio of UTI Nifty Fund?",
        ):
            response = client.post(
                f"/api/threads/{client.post('/api/threads').json()['thread_id']}/messages",
                json={"text": text},
            )
            assert response.status_code == 200
    assert constructed == []


def test_grounded_call_uses_temperature_zero_and_only_this_question(database_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_value")
    recorded: list[dict] = []
    content = {
        "sentences": ["Use the published capital gains steps on that page."],
        "supported": True,
    }

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            recorded.append({"url": url, "headers": headers, "json": json})
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json_lib.dumps(content)}}]},
                request=request,
            )

    monkeypatch.setattr("app.rag.generate.httpx.Client", Client)
    monkeypatch.setattr("app.threads.retrieve", lambda *args, **kwargs: _guide_retrieval())
    application = create_app()
    with TestClient(application) as client:
        _stamp(database_path)
        thread_id = client.post("/api/threads").json()["thread_id"]
        advice = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"text": "Should I invest in UTI Nifty Fund?"},
        )
        assert advice.status_code == 200
        assert recorded == []
        question = "How do I download a capital gains statement for UTI Nifty Fund?"
        answer = client.post(f"/api/threads/{thread_id}/messages", json={"text": question})
    assert answer.status_code == 200
    body = answer.json()
    assert body["kind"] == "answer"
    assert "Use the published capital gains steps on that page." in body["text"]
    assert "Open the AMFI download page" not in body["text"]
    assert body["citation"]["url"] == GUIDE_URL
    assert "groww.in" not in body["text"]
    assert len(recorded) == 1
    sent = recorded[0]
    assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer gsk_test_secret_value"
    assert sent["json"]["temperature"] == 0
    assert type(sent["json"]["temperature"]) is int
    assert sent["json"]["model"] == "llama-3.3-70b-versatile"
    rendered = str(sent["json"]["messages"])
    assert question in rendered
    assert NIFTY.name in rendered
    assert GUIDE_TEXT in rendered
    assert "does not provide investment advice" not in rendered
    assert "Should I invest" not in rendered
    assert "gsk_test_secret_value" not in rendered


def test_bad_model_payloads_are_replaced(database_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_value")
    payload = {"raw": ""}

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            request = httpx.Request("POST", url)
            if payload["raw"] == "timeout":
                raise httpx.TimeoutException("gsk_test_secret_value")
            if payload["raw"] == "status":
                return httpx.Response(500, json={"error": "no"}, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": payload["raw"]}}]},
                request=request,
            )

    monkeypatch.setattr("app.rag.generate.httpx.Client", Client)
    monkeypatch.setattr("app.threads.retrieve", lambda *args, **kwargs: _guide_retrieval())
    application = create_app()
    cases = {
        "four": (
            '{"sentences": ["One.", "Two.", "Three.", "FOURTH-SENTENCE-MARKER."], "supported": true}',
            "answer",
            "FOURTH-SENTENCE-MARKER",
        ),
        "groww": (
            '{"sentences": ["See https://groww.in/mutual-funds/uti-nifty-fund-direct-growth now."], "supported": true}',
            "answer",
            "groww.in",
        ),
        "percent": (
            '{"sentences": ["The fee is 9.5%."], "supported": true}',
            "answer",
            "9.5%",
        ),
        "recommend": (
            '{"sentences": ["You should buy this scheme."], "supported": true}',
            "answer",
            "buy",
        ),
        "pii": (
            '{"sentences": ["Contact ABCDE1234F for the form."], "supported": true}',
            "refusal",
            PAN,
        ),
        "unsupported": (
            '{"sentences": ["The fee is 9%."], "supported": false}',
            "not_in_corpus",
            "9%",
        ),
        "empty": (
            '{"sentences": [], "supported": true}',
            "not_in_corpus",
            "9%",
        ),
        "invalid": (
            "not json",
            "not_in_corpus",
            "not json",
        ),
    }
    with TestClient(application) as client:
        _stamp(database_path)
        for name, (raw, kind, marker) in cases.items():
            payload["raw"] = raw
            body = _ask(client, "How do I download a mutual fund capital gains statement?")
            assert body["kind"] == kind, name
            assert marker not in body["text"], name
            assert body["citation"]["url"] != ""
            assert "groww.in" not in body["citation"]["url"]
            assert body["footer"] == f"Last updated from sources: {DATE}"
            if kind == "answer":
                assert "Open the AMFI download page" in body["text"]
            if kind == "refusal":
                assert "sebi.gov.in" in body["citation"]["url"]
        payload["raw"] = "timeout"
        timed = _ask(client, "How do I download a mutual fund capital gains statement?")
        assert timed["kind"] == "answer"
        assert "Open the AMFI download page" in timed["text"]
        assert "gsk_test_secret_value" not in timed["text"]
        payload["raw"] = "status"
        failed = _ask(client, "How do I download a mutual fund capital gains statement?")
        assert failed["kind"] == "answer"
        assert failed["citation"]["url"] == GUIDE_URL
        assert "Open the AMFI download page" in failed["text"]


def test_timeout_does_not_log_the_api_key(database_path: Path, monkeypatch, caplog):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_value")

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            raise httpx.TimeoutException("gsk_test_secret_value")

    monkeypatch.setattr("app.rag.generate.httpx.Client", Client)
    monkeypatch.setattr("app.threads.retrieve", lambda *args, **kwargs: _guide_retrieval())
    application = create_app()
    with caplog.at_level(logging.DEBUG):
        with TestClient(application) as client:
            _stamp(database_path)
            body = _ask(client, "How do I download a mutual fund capital gains statement?")
    assert body["kind"] == "answer"
    assert "gsk_test_secret_value" not in caplog.text
    assert "gsk_test_secret_value" not in body["text"]


def test_model_footer_date_is_replaced_by_the_chunk_date(database_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_value")

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            request = httpx.Request("POST", url)
            raw = (
                '{"sentences": ["Use the published capital gains steps on that page.", '
                '"Last updated from sources: 1 Jan 1999"], "supported": true}'
            )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": raw}}]},
                request=request,
            )

    monkeypatch.setattr("app.rag.generate.httpx.Client", Client)
    monkeypatch.setattr("app.threads.retrieve", lambda *args, **kwargs: _guide_retrieval())
    application = create_app()
    with TestClient(application) as client:
        _stamp(database_path)
        body = _ask(client, "How do I download a mutual fund capital gains statement?")
    assert body["footer"] == f"Last updated from sources: {DATE}"
    assert "1 Jan 1999" not in body["text"]
    assert "1 Jan 1999" not in body["footer"]


def test_validator_rejections_become_the_safe_template():
    fallback = _fallback()
    rejected = {
        "four sentences": _draft(
            text="One. Two. Three. Four. " + NIFTY.factsheet_url,
        ),
        "groww url": _draft(
            text="See https://groww.in/mutual-funds/uti-nifty-fund-direct-growth.",
            citation_url="https://groww.in/mutual-funds/uti-nifty-fund-direct-growth",
        ),
        "second url": _draft(
            text=f"See {NIFTY.factsheet_url} and {ELSS.factsheet_url}.",
        ),
        "absent percent": _draft(
            text=f"The ratio is 9%. {NIFTY.factsheet_url}",
            cited_text="no figures here",
        ),
        "missing footer": _draft(footer=""),
        "recommend": _draft(text=f"You should buy this scheme. {NIFTY.factsheet_url}"),
        "pii echo": _draft(text=f"The PAN is {PAN}. {NIFTY.factsheet_url}"),
    }
    for name, draft in rejected.items():
        checked = validate(draft, CORPUS)
        assert checked.ok is False, name
        resolved = resolve(draft, CORPUS, fallback)
        assert resolved.text == fallback.text
        assert resolved.footer == fallback.footer
        assert "9%" not in resolved.text
        assert PAN not in resolved.text
        assert "groww.in" not in resolved.text
        assert "Four." not in resolved.text


def test_footer_date_can_be_overwritten_and_a_missing_footer_cannot():
    assert replace_footer_date("Last updated from sources: 1 Jan 1999", DATE) == (
        f"Last updated from sources: {DATE}"
    )
    assert replace_footer_date("", DATE) is None
    assert replace_footer_date("Last updated from sources: 1 Jan 1999\nLast updated from sources: 2 Jan 1999", DATE) is None
    wrong = _draft(footer="Last updated from sources: 1 Jan 1999")
    assert validate(wrong, CORPUS).ok is False
    assert "footer" in validate(wrong, CORPUS).reasons


def test_abbreviations_do_not_inflate_the_sentence_count():
    rupees = (
        "The minimum SIP stated for UTI Nifty Fund Direct Growth is 'Rs. 500'. "
        "This wording is from the scheme factsheet."
    )
    assert sentence_count(rupees) == 2
    explained = (
        "Costs, e.g. the exit load, are listed. "
        "See the line, i.e. the schedule. "
        "The page names the fact."
    )
    assert sentence_count(explained) == 3


def test_scheme_name_digits_are_allowed_and_other_digits_are_not():
    named = _draft(
        text=(
            "The benchmark stated for UTI Unit Scheme 1986 Mastershare Direct Growth is 'the index'. "
            f"{NIFTY.factsheet_url}"
        ),
        scheme_name="UTI Unit Scheme 1986 Mastershare Direct Growth",
        cited_text="the index",
    )
    assert validate(named, CORPUS).ok, validate(named, CORPUS).reasons
    invented = _draft(
        text=f"The benchmark stated for the scheme is '1986'. {NIFTY.factsheet_url}",
        scheme_name=NIFTY.name,
        cited_text="the index",
    )
    assert validate(invented, CORPUS).ok is False
    assert "figures" in validate(invented, CORPUS).reasons


def test_unicode_percent_needs_the_same_digits_in_the_chunk():
    grounded = _draft(
        text=f"The stated fee is 1％. {NIFTY.factsheet_url}",
        cited_text="costs 1 percent of assets",
    )
    assert validate(grounded, CORPUS).ok, validate(grounded, CORPUS).reasons
    missing = _draft(
        text=f"The stated fee is 2％. {NIFTY.factsheet_url}",
        cited_text="costs 1 percent of assets",
    )
    assert validate(missing, CORPUS).ok is False


def test_prompt_has_no_history_parameter_and_stops_at_four_chunks():
    assert "history" not in inspect.signature(build_messages).parameters
    chunks = tuple(
        ChunkHit(
            chunk_id=f"c{index}",
            url=GUIDE_URL,
            scheme_id=None,
            doc_type="statement_guide",
            title=f"Title {index}",
            page=None,
            heading=None,
            text=f"CHUNK-{index} body",
            retrieved_at=RETRIEVED,
            score=0.9,
        )
        for index in range(1, 6)
    )
    settings = Settings(
        groq_api_key="gsk_test_secret_value",
        groq_model="llama-3.3-70b-versatile",
        groq_base_url="https://api.groq.com/openai/v1",
        database_path=Path("data/assistant.db"),
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
    )
    body = chat_request("How do I download a capital gains statement?", NIFTY.name, chunks, settings)
    assert body["temperature"] == 0
    rendered = str(body["messages"])
    assert "How do I download a capital gains statement?" in rendered
    assert NIFTY.name in rendered
    for index in range(1, 5):
        assert f"CHUNK-{index} body" in rendered
    assert "CHUNK-5 body" not in rendered
    assert "EARLIER ANSWER" not in rendered
    assert "gsk_test_secret_value" not in rendered

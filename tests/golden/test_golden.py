"""Phase 9 golden suite. Posts the fixture offline and checks the success list."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from app.db import display_date, read_db, transaction
from app.rag.templates import sentence_count
from app.schemes import ALLOWLIST_HOSTS, load_corpus
from app.threads import REDACTED_USER_TEXT

GOLDEN = Path(__file__).resolve().parent
ANSWER_FIELDS = frozenset(
    {
        "expense_ratio",
        "exit_load",
        "minimum_sip",
        "lock_in",
        "riskometer",
        "benchmark",
    }
)


def _load_questions() -> list[dict]:
    rows: list[dict] = []
    for line_number, raw in enumerate(
        (GOLDEN / "questions.jsonl").read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        row["_line"] = line_number
        rows.append(row)
    return rows


def _load_facts() -> dict:
    return json.loads((GOLDEN / "facts.json").read_text(encoding="utf-8"))


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _host_matches(url: str, expected: str) -> bool:
    host = _host(url)
    return host == expected or host.endswith("." + expected)


def _label(row: dict) -> str:
    return f"{row['id']} (line {row['_line']})"


def test_golden_file_covers_the_minimum_set():
    corpus = load_corpus()
    rows = _load_questions()
    facts = {
        (fact["scheme_id"], fact["field"]): fact["value_text"]
        for fact in _load_facts()["facts"]
    }
    assert rows, "questions.jsonl is empty"
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids))

    for row in rows:
        assert row["thread"] and row["question"] and row["expect_kind"], _label(row)
        has_scheme_host = "expect_scheme_id" in row and "expect_host" in row
        has_substr = bool(row.get("expect_url_substr"))
        assert has_scheme_host or has_substr, _label(row)
        if row.get("field") and row["expect_kind"] == "answer":
            value = facts[(row["expect_scheme_id"], row["field"])]
            assert value in _as_list(row.get("expect_contains")), _label(row)

    answered = [
        row for row in rows if row.get("field") in ANSWER_FIELDS and row["expect_kind"] == "answer"
    ]
    assert {row["field"] for row in answered} == ANSWER_FIELDS
    scheme_ids = {row["expect_scheme_id"] for row in answered}
    assert "uti-elss" in scheme_ids
    assert "uti-short-term" in scheme_ids or "uti-silver-etf-fof" in scheme_ids
    categories = {corpus.scheme(scheme_id).category for scheme_id in scheme_ids}
    assert len(categories) >= 3

    by_id = {row["id"]: row for row in rows}
    assert by_id["advice"]["question"] == "Should I invest in UTI Small Cap Fund?"
    assert by_id["advice"]["expect_kind"] == "refusal"
    assert by_id["comparison"]["question"] == "Which is better, UTI Nifty or UTI Mid Cap?"
    assert by_id["comparison"]["expect_kind"] == "refusal"
    assert by_id["comparison"]["expect_scheme_id"] is None
    performance = by_id["performance"]
    assert performance["question"] == "What is the 5-year return of UTI Equity Fund?"
    assert performance["expect_kind"] == "performance_redirect"
    assert performance["expect_host"] == "utimf.com"
    assert performance["forbid_percent"] is True
    assert by_id["capital-gains"]["expect_kind"] in {"answer", "not_in_corpus"}
    assert by_id["out-of-scope"]["question"] == "What is the exit load of HDFC Flexi Cap?"
    assert by_id["out-of-scope"]["expect_kind"] == "refusal"
    assert "ABCDE1234F" in by_id["pan"]["question"]
    assert by_id["pan"]["absent_from_messages"] == "ABCDE1234F"
    assert by_id["pan"]["expect_kind"] == "refusal"

    followups = [row for row in rows if row.get("followup")]
    assert {row["expect_scheme_id"] for row in followups} == {"uti-elss", "uti-nifty"}
    assert {row["question"] for row in followups} == {"What is the expense ratio?"}
    for followup in followups:
        earlier = [
            row
            for row in rows
            if row["thread"] == followup["thread"] and row["_line"] < followup["_line"]
        ]
        assert earlier, followup["id"]
        assert earlier[0]["expect_scheme_id"] == followup["expect_scheme_id"]

    missing = by_id["missing-benchmark"]
    assert missing["expect_kind"] == "not_in_corpus"
    dropped = facts[(missing["drop_fact"]["scheme_id"], missing["drop_fact"]["field"])]
    assert dropped in _as_list(missing.get("expect_absent"))
    assert "%" in _as_list(missing.get("expect_absent"))


def test_golden_runner_posts_each_question(client, database_path: Path, monkeypatch):
    monkeypatch.setattr("app.rag.retrieve._query_vector", _refuse_network)
    monkeypatch.setattr("app.rag.generate.complete_chat", _refuse_network)
    assert client.app.state.settings.groq_api_key == ""

    payload = _load_facts()
    corpus = load_corpus()
    official_urls = {document.url for document in corpus.documents}
    _seed(database_path, payload, official_urls)
    footer = f"Last updated from sources: {display_date(payload['retrieved_at'])}"
    threads: dict[str, str] = {}

    for row in _load_questions():
        _drop_fact(database_path, row.get("drop_fact"))
        thread_id = threads.setdefault(row["thread"], _open_thread(client))
        response = client.post(
            f"/api/threads/{thread_id}/messages",
            json={"text": row["question"]},
        )
        assert response.status_code == 200, f"{_label(row)}: {response.text}"
        _assert_reply(row, response.json(), footer, official_urls)
        if row.get("absent_from_messages"):
            _assert_secret_absent(database_path, row["absent_from_messages"])

    _assert_threads_stay_separate(client, threads)


def _refuse_network(*_args, **_kwargs):
    raise AssertionError("golden suite must not embed a query or call Groq")


def _seed(path: Path, payload: dict, official_urls: set[str]) -> None:
    stamp = payload["retrieved_at"]
    facts = payload["facts"]
    with transaction(path) as conn:
        conn.execute(
            """
            INSERT INTO ingest_state (id, last_ingest, documents_ok, documents_failed)
            VALUES (1, ?, ?, 0)
            """,
            (stamp, len(facts)),
        )
        for fact in facts:
            assert fact["source_url"] in official_urls, fact["source_url"]
            conn.execute(
                """
                INSERT INTO scheme_facts (
                    scheme_id, field, value_text, source_url, page, retrieved_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (
                    fact["scheme_id"],
                    fact["field"],
                    fact["value_text"],
                    fact["source_url"],
                    stamp,
                ),
            )


def _drop_fact(path: Path, spec: dict | None) -> None:
    if not spec:
        return
    with transaction(path) as conn:
        cursor = conn.execute(
            "DELETE FROM scheme_facts WHERE scheme_id = ? AND field = ?",
            (spec["scheme_id"], spec["field"]),
        )
        assert cursor.rowcount == 1, spec


def _open_thread(client) -> str:
    response = client.post("/api/threads")
    assert response.status_code == 200, response.text
    return response.json()["thread_id"]


def _assert_reply(row: dict, body: dict, footer: str, official_urls: set[str]) -> None:
    label = _label(row)
    assert body["kind"] == row["expect_kind"], label
    if "expect_scheme_id" in row:
        assert body["scheme_id"] == row["expect_scheme_id"], label
    citation = body["citation"]["url"]
    assert citation in official_urls, label
    assert _host(citation) in ALLOWLIST_HOSTS, label
    assert "groww.in" not in citation, label
    assert "groww.in" not in body["text"], label
    if row.get("expect_host"):
        assert _host_matches(citation, row["expect_host"]), f"{label}: {citation}"
    if row.get("expect_url_substr"):
        assert row["expect_url_substr"] in citation, f"{label}: {citation}"
    assert body["text"].count("http") == 1, label
    assert citation in body["text"], label
    assert 1 <= sentence_count(body["text"]) <= 3, f"{label}: {body['text']}"
    assert body["footer"] == footer, label
    assert footer not in body["text"], label
    for snippet in _as_list(row.get("expect_contains")):
        assert snippet in body["text"], f"{label}: missing {snippet!r}"
    for snippet in _as_list(row.get("expect_absent")):
        assert snippet not in body["text"], f"{label}: unexpected {snippet!r}"
    if row.get("forbid_percent"):
        assert "%" not in body["text"], label


def _assert_secret_absent(path: Path, secret: str) -> None:
    with read_db(path) as conn:
        rows = conn.execute("SELECT role, text, citation_url, footer FROM messages").fetchall()
    blob = " ".join(" ".join(str(value or "") for value in row) for row in rows)
    assert secret not in blob
    assert any(row["role"] == "user" and row["text"] == REDACTED_USER_TEXT for row in rows)


def _assert_threads_stay_separate(client, threads: dict[str, str]) -> None:
    elss = _transcript(client, threads["elss"])
    nifty = _transcript(client, threads["nifty"])
    assert "Direct plan TER 1.12%" in elss
    assert "Direct plan TER 0.18%" not in elss
    assert "Lock-in-period of 3 years" in elss
    assert "Direct plan TER 0.18%" in nifty
    assert "Direct plan TER 1.12%" not in nifty
    assert "Lock-in-period of 3 years" not in nifty
    elss_detail = client.get(f"/api/threads/{threads['elss']}").json()
    nifty_detail = client.get(f"/api/threads/{threads['nifty']}").json()
    assert elss_detail["active_scheme_id"] == "uti-elss"
    assert nifty_detail["active_scheme_id"] == "uti-nifty"


def _transcript(client, thread_id: str) -> str:
    response = client.get(f"/api/threads/{thread_id}")
    assert response.status_code == 200, response.text
    return "\n".join(message["text"] for message in response.json()["messages"])

"""Phase 4: parse, chunk, and fact extraction without the network.

Embeddings are a local stub so these tests do not download a model.
"""

import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect, display_date
from app.ingest.chunk import TOKEN_OVERLAP, TOKEN_SIZE, Block, chunk_blocks
from app.ingest.extract_facts import extract_facts
from app.ingest.fetch import FetchError, fetch_url
from app.ingest.parse import parse_document
from app.ingest.run import run_ingest
from app.main import create_app
from app.rag.embed import pack_embedding
from app.schemes import Document

EXIT_LOAD = "1% if redeemed within 365 days"
NIFTY_URL = "https://www.utimf.com/mutual-funds/uti-nifty-50-index-fund"
PDF_URL = "https://www.utimf.com/schemes/uti-nifty-factsheet.pdf"
KEEPER_URL = "https://www.utimf.com/mutual-funds/uti-mid-cap-fund"
OTHER_URL = "https://www.amfiindia.com/investor/knowledge-center-info?zoneName=riskInMutualFunds"
SHARED_URL = "https://investor.sebi.gov.in/understanding_mf.html"


def test_html_and_pdf_extract_the_exit_load_span_unchanged(database_path: Path):
    html = _page(
        "<h2>Exit Load</h2>",
        f"<p>{EXIT_LOAD}</p>",
        "<h2>About the scheme</h2>",
        f"<p>{'rules ' * 30}The factsheet states the published rule for this scheme.</p>",
    )
    pdf = _pdf([[], ["Exit Load", EXIT_LOAD]])
    routes = {
        NIFTY_URL: httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"}),
        PDF_URL: httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"}),
    }
    summary = run_ingest(
        database_path,
        documents=[_scheme_doc(NIFTY_URL), _scheme_doc(PDF_URL, title="Nifty factsheet PDF")],
        client=_client(routes),
    )
    assert summary.documents_ok == 2
    assert summary.failures == ()

    conn = connect(database_path)
    try:
        html_fact = conn.execute(
            """
            SELECT value_text, page FROM scheme_facts
            WHERE source_url = ? AND field = 'exit_load'
            """,
            (NIFTY_URL,),
        ).fetchone()
        pdf_fact = conn.execute(
            """
            SELECT value_text, page FROM scheme_facts
            WHERE source_url = ? AND field = 'exit_load'
            """,
            (PDF_URL,),
        ).fetchone()
        assert html_fact["value_text"] == EXIT_LOAD
        assert "0.01" not in html_fact["value_text"]
        assert EXIT_LOAD in pdf_fact["value_text"]
        assert pdf_fact["page"] == 2
        chunk = conn.execute(
            """
            SELECT url, scheme_id, doc_type, title, retrieved_at, text, embedding
            FROM chunks WHERE url = ?
            """,
            (NIFTY_URL,),
        ).fetchone()
        assert chunk["url"] == NIFTY_URL
        assert chunk["scheme_id"] == "uti-nifty"
        assert chunk["doc_type"] == "factsheet"
        assert chunk["title"] == "UTI Nifty 50 Index Fund"
        assert chunk["retrieved_at"]
        assert EXIT_LOAD in chunk["text"]
        assert chunk["embedding"] == _fake_embed_texts([chunk["text"]])[0]
        assert display_date(chunk["retrieved_at"])
    finally:
        conn.close()


def test_redirect_to_groww_writes_no_chunk(database_path: Path):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "groww.in" in str(request.url):
            return httpx.Response(200, content=b"should not be fetched")
        return httpx.Response(
            302,
            headers={"location": "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth"},
        )

    summary = run_ingest(
        database_path,
        documents=[_scheme_doc(NIFTY_URL)],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert summary.documents_ok == 0
    assert any("groww.in" in failure.reason for failure in summary.failures)
    assert all("groww.in" not in url for url in requested)
    conn = connect(database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheme_facts").fetchone()[0] == 0
    finally:
        conn.close()


def test_ingest_leaves_an_existing_thread_in_place(database_path: Path, client: TestClient):
    created = client.post("/api/threads").json()
    posted = client.post(
        f"/api/threads/{created['thread_id']}/messages",
        json={"text": "keep me"},
    )
    assert posted.status_code == 200
    run_ingest(
        database_path,
        documents=[_scheme_doc(NIFTY_URL)],
        client=_client({NIFTY_URL: _html_response(_exit_page())}),
    )
    detail = client.get(f"/api/threads/{created['thread_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["title"] == "New conversation"
    assert body["active_scheme_id"] is None
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["text"] == "keep me"
    assert body["messages"][1]["role"] == "assistant"
    assert "keep me" not in body["messages"][1]["text"]
    conn = connect(database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
    finally:
        conn.close()


def test_failed_url_does_not_delete_other_urls_chunks(database_path: Path):
    run_ingest(
        database_path,
        documents=[_scheme_doc(KEEPER_URL, scheme_id="uti-mid-cap", title="UTI Mid Cap Fund")],
        client=_client({KEEPER_URL: _html_response(_exit_page())}),
    )
    failing = "https://www.utimf.com/downloads/sid"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sid"):
            return httpx.Response(302, headers={"location": "https://groww.in/sid"})
        if "groww.in" in request.url.host:
            return httpx.Response(200, content=b"nope")
        return _html_response(_page("<h2>Benchmark</h2>", "<p>Nifty 500 TRI</p>", "<p>" + ("guide " * 20) + "</p>"))

    summary = run_ingest(
        database_path,
        documents=[
            Document(url=failing, scheme_id=None, doc_type="sid", title="SID library"),
            Document(url=OTHER_URL, scheme_id=None, doc_type="amfi", title="AMFI education"),
        ],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert summary.documents_ok == 1
    assert summary.documents_ok + len(summary.failures) == 2
    conn = connect(database_path)
    try:
        keeper = conn.execute("SELECT text FROM chunks WHERE url = ?", (KEEPER_URL,)).fetchall()
        assert keeper
        assert any(EXIT_LOAD in row["text"] for row in keeper)
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE url = ?", (failing,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE url = ?", (OTHER_URL,)).fetchone()[0] > 0
    finally:
        conn.close()


def test_failed_refetch_keeps_the_stored_document(database_path: Path):
    document = _scheme_doc(NIFTY_URL)
    run_ingest(
        database_path,
        documents=[document],
        client=_client({NIFTY_URL: _html_response(_exit_page())}),
    )
    summary = run_ingest(
        database_path,
        documents=[document],
        client=_client({NIFTY_URL: httpx.Response(500, content=b"down")}),
    )
    assert summary.documents_ok == 0
    conn = connect(database_path)
    try:
        rows = conn.execute("SELECT text FROM chunks WHERE url = ?", (NIFTY_URL,)).fetchall()
        assert rows
        assert any(EXIT_LOAD in row["text"] for row in rows)
        fact = conn.execute(
            "SELECT value_text FROM scheme_facts WHERE source_url = ? AND field = 'exit_load'",
            (NIFTY_URL,),
        ).fetchone()
        assert fact["value_text"] == EXIT_LOAD
    finally:
        conn.close()


def test_reingest_replaces_chunks_for_that_url_only(database_path: Path):
    first = _scheme_doc(NIFTY_URL)
    second_url = KEEPER_URL
    run_ingest(
        database_path,
        documents=[first, _scheme_doc(second_url, scheme_id="uti-mid-cap", title="UTI Mid Cap Fund")],
        client=_client(
            {
                NIFTY_URL: _html_response(_exit_page()),
                second_url: _html_response(_page("<h2>Exit Load</h2>", "<p>Nil</p>", "<p>" + ("mid " * 20) + "</p>")),
            }
        ),
    )
    replacement = "2% if redeemed within 180 days"
    run_ingest(
        database_path,
        documents=[first],
        client=_client({NIFTY_URL: _html_response(_page("<h2>Exit Load</h2>", f"<p>{replacement}</p>"))}),
    )
    conn = connect(database_path)
    try:
        nifty = " ".join(
            row["text"] for row in conn.execute("SELECT text FROM chunks WHERE url = ?", (NIFTY_URL,))
        )
        assert replacement in nifty
        assert EXIT_LOAD not in nifty
        mid = " ".join(
            row["text"] for row in conn.execute("SELECT text FROM chunks WHERE url = ?", (second_url,))
        )
        assert "Nil" in mid
        fact = conn.execute(
            "SELECT value_text FROM scheme_facts WHERE source_url = ? AND field = 'exit_load'",
            (NIFTY_URL,),
        ).fetchone()
        assert fact["value_text"] == replacement
    finally:
        conn.close()


def test_null_scheme_stores_chunks_and_no_facts(database_path: Path):
    page = _page("<h2>Exit Load</h2>", f"<p>{EXIT_LOAD}</p>", "<p>" + ("education " * 20) + "</p>")
    run_ingest(
        database_path,
        documents=[Document(url=SHARED_URL, scheme_id=None, doc_type="sebi", title="SEBI education")],
        client=_client({SHARED_URL: _html_response(page)}),
    )
    conn = connect(database_path)
    try:
        chunk = conn.execute("SELECT scheme_id, text FROM chunks WHERE url = ?", (SHARED_URL,)).fetchone()
        assert chunk["scheme_id"] is None
        assert EXIT_LOAD in chunk["text"]
        assert conn.execute(
            "SELECT COUNT(*) FROM scheme_facts WHERE source_url = ?",
            (SHARED_URL,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_missing_lock_in_span_is_not_invented(database_path: Path):
    page = _page("<h2>Exit Load</h2>", "<p>Nil</p>", "<p>" + ("scheme " * 20) + "with no statutory period.</p>")
    run_ingest(
        database_path,
        documents=[_scheme_doc(NIFTY_URL)],
        client=_client({NIFTY_URL: _html_response(page)}),
    )
    conn = connect(database_path)
    try:
        fields = {
            row["field"]
            for row in conn.execute("SELECT field, value_text FROM scheme_facts WHERE source_url = ?", (NIFTY_URL,))
        }
        assert "exit_load" in fields
        assert "lock_in" not in fields
        assert conn.execute(
            "SELECT COUNT(*) FROM scheme_facts WHERE value_text LIKE '%3 years%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_lock_in_span_is_copied_from_the_exit_load(database_path: Path):
    source = "Nil (Lock-in-period of 3 years from the date of for each investment)"
    page = _page("<h2>Exit Load</h2>", f"<p>{source}</p>")
    run_ingest(
        database_path,
        documents=[_scheme_doc("https://www.utimf.com/mutual-funds/uti-elss-tax-saver-fund", scheme_id="uti-elss", title="ELSS")],
        client=_client(
            {
                "https://www.utimf.com/mutual-funds/uti-elss-tax-saver-fund": _html_response(page),
            }
        ),
    )
    conn = connect(database_path)
    try:
        exit_load = conn.execute(
            "SELECT value_text FROM scheme_facts WHERE field = 'exit_load'"
        ).fetchone()["value_text"]
        lock_in = conn.execute(
            "SELECT value_text FROM scheme_facts WHERE field = 'lock_in'"
        ).fetchone()["value_text"]
        assert exit_load == source
        assert "Lock-in-period of 3 years from the date of for each investment" in lock_in
        assert lock_in in source
    finally:
        conn.close()


def test_minimum_investment_is_lumpsum_not_sip(database_path: Path):
    page = """
    <html><body>
      <div class="scheme-display">
        <span class="nav-label">Minimum Investment</span>
        <span class="label-1">₹1,000</span>
      </div>
      <p>Special Facilities SWP/SIP/STP are offered on this scheme for investors.</p>
    </body></html>
    """
    run_ingest(
        database_path,
        documents=[_scheme_doc(NIFTY_URL)],
        client=_client({NIFTY_URL: _html_response(page)}),
    )
    conn = connect(database_path)
    try:
        rows = {
            row["field"]: row["value_text"]
            for row in conn.execute("SELECT field, value_text FROM scheme_facts WHERE source_url = ?", (NIFTY_URL,))
        }
        assert rows["minimum_lumpsum"] == "₹1,000"
        assert "minimum_sip" not in rows
    finally:
        conn.close()


def test_health_reports_ingest_counts(database_path: Path, client: TestClient):
    before = client.get("/api/health").json()
    assert before == {"ok": True, "schemes": 10, "documents": 0, "last_ingest": None}
    run_ingest(
        database_path,
        documents=[_scheme_doc(NIFTY_URL)],
        client=_client({NIFTY_URL: _html_response(_exit_page())}),
    )
    after = client.get("/api/health").json()
    assert after["ok"] is True
    assert after["schemes"] == 10
    assert after["documents"] == 1
    assert after["last_ingest"]
    assert display_date(after["last_ingest"])


def test_allowlisted_redirect_is_followed_and_cited_as_the_manifest_url(database_path: Path):
    start = "https://www.utimf.com/start"
    landed = "https://www.utimf.com/landed"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).rstrip("/") == start:
            return httpx.Response(302, headers={"location": landed})
        return _html_response(_exit_page())

    run_ingest(
        database_path,
        documents=[Document(url=start, scheme_id="uti-nifty", doc_type="factsheet", title="Start")],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    conn = connect(database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE url = ?", (start,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE url = ?", (landed,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT value_text FROM scheme_facts WHERE source_url = ?",
            (start,),
        ).fetchone()["value_text"] == EXIT_LOAD
    finally:
        conn.close()


def test_fetch_rejects_a_body_over_the_size_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 80)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchError, match="size cap"):
        fetch_url(NIFTY_URL, client, max_bytes=30)


def test_fact_blocks_stay_whole_and_prose_windows_overlap():
    words = [f"w{index}" for index in range(30)]
    prose = " ".join(words)
    fact = f"Exit Load\n{EXIT_LOAD} " + prose
    drafts = chunk_blocks(
        [
            Block(heading="Exit Load", text=fact, page=3),
            Block(heading=None, text=prose, page=None),
        ],
        size=10,
        overlap=4,
    )
    assert drafts[0].text == fact
    assert drafts[0].page == 3
    windows = [draft.text.split() for draft in drafts if draft.heading is None]
    assert len(windows) >= 2
    assert windows[0][-4:] == windows[1][:4]
    assert TOKEN_SIZE == 800
    assert TOKEN_OVERLAP == 120


def test_parse_chunk_and_extract_do_not_need_the_network():
    html = _exit_page()
    blocks = parse_document(html, "text/html", NIFTY_URL)
    drafts = chunk_blocks(blocks)
    facts = extract_facts(drafts, "uti-nifty")
    assert any(fact.field == "exit_load" and fact.value_text == EXIT_LOAD for fact in facts)
    pdf_blocks = parse_document(_pdf([["Exit Load", EXIT_LOAD]]), "application/pdf", PDF_URL)
    pdf_facts = extract_facts(chunk_blocks(pdf_blocks), "uti-nifty")
    assert any(fact.field == "exit_load" and EXIT_LOAD in fact.value_text for fact in pdf_facts)


def test_display_date_uses_english_month_names():
    assert display_date("2026-10-25T09:00:00.000000Z") == "25 Oct 2026"
    assert display_date("2026-10-05T00:00:00Z") == "5 Oct 2026"


def _fake_embed_texts(texts: list[str]) -> list[bytes]:
    blobs = []
    for text in texts:
        digest = hashlib.sha256(text.encode()).digest()
        values = [byte / 255.0 for byte in digest[:8]]
        blobs.append(pack_embedding(values))
    return blobs


@pytest.fixture(autouse=True)
def _offline_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingest.run.embed_texts", _fake_embed_texts)


def _scheme_doc(url: str, scheme_id: str = "uti-nifty", title: str = "UTI Nifty 50 Index Fund") -> Document:
    return Document(url=url, scheme_id=scheme_id, doc_type="factsheet", title=title)


def _exit_page() -> bytes:
    return _page(
        "<h2>Exit Load</h2>",
        f"<p>{EXIT_LOAD}</p>",
        "<h2>Notes</h2>",
        "<p>" + ("published " * 20) + "wording from the factsheet.</p>",
    )


def _page(*parts: str) -> bytes:
    body = "".join(parts)
    return f"<!DOCTYPE html><html><head><title>Factsheet</title></head><body>{body}</body></html>".encode()


def _html_response(page: bytes | str) -> httpx.Response:
    content = page if isinstance(page, bytes) else page.encode()
    return httpx.Response(200, content=content, headers={"content-type": "text/html; charset=utf-8"})


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes[str(request.url)]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _pdf(pages: list[list[str]]) -> bytes:
    count = len(pages)
    font_id = 3 + count * 2
    objects: list[bytes] = [b""] * (font_id + 1)
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(count))
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Count {count} /Kids [{kids}] >>".encode()
    for index, lines in enumerate(pages):
        page_id = 3 + index * 2
        content_id = page_id + 1
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()
        if lines:
            commands = ["BT", "/F1 12 Tf", "72 720 Td"]
            for line_index, line in enumerate(lines):
                escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                if line_index:
                    commands.append("0 -18 Td")
                commands.append(f"({escaped}) Tj")
            commands.append("ET")
            stream = ("\n".join(commands) + "\n").encode()
        else:
            stream = b""
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream"
        )
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number in range(1, font_id + 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(objects[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {font_id + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for number in range(1, font_id + 1):
        output.extend(f"{offsets[number]:010d} 00000 n \n".encode())
    output.extend(
        f"trailer << /Size {font_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)

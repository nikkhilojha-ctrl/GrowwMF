"""Phase 8: the page renders the contract, and Groww stays a reference link."""

from app.schemes import load_corpus


def test_index_has_disclaimer_welcome_and_examples(client):
    page = client.get("/")
    assert page.status_code == 200
    text = page.text
    assert "Facts-only. No investment advice." in text
    assert "It refuses investment advice and comparisons." in text
    assert "What is the expense ratio of UTI Nifty Fund Direct Growth?" in text
    assert (
        "What is the ELSS lock-in period for UTI Long Term Equity Fund "
        "(Tax Saving) Direct Growth?"
    ) in text
    assert "How do I download a mutual fund capital gains statement?" in text
    assert "Do not paste PAN, Aadhaar, account numbers, OTPs, email, or phone numbers." in text
    assert 'id="groww-link"' in text
    assert "View on Groww" in text
    assert 'maxlength="1000"' in text
    assert client.get("/styles.css").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_page_script_does_not_store_messages(client):
    script = client.get("/app.js").text
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "document.cookie" not in script
    assert "scheme.groww_url" in script
    assert "message.citation.url" in script


def test_schemes_endpoint_returns_groww_references_only(client):
    response = client.get("/api/schemes")
    assert response.status_code == 200
    rows = response.json()
    corpus = load_corpus()
    assert [row["scheme_id"] for row in rows] == [scheme.scheme_id for scheme in corpus.schemes]
    for row, scheme in zip(rows, corpus.schemes, strict=True):
        assert set(row) == {"scheme_id", "name", "category", "groww_url"}
        assert row["name"] == scheme.name
        assert row["category"] == scheme.category
        assert row["groww_url"] == scheme.groww_url
        assert row["groww_url"].startswith("https://groww.in/")

"""Phase 2: ten schemes, an allowlisted manifest, and rejection of anything else."""

from pathlib import Path

import pytest
import yaml

from app.schemes import CorpusLoadError, load_corpus

EXPECTED_IDS = (
    "uti-nifty",
    "uti-small-cap",
    "uti-large-mid-cap",
    "uti-equity",
    "uti-mastershare",
    "uti-mid-cap",
    "uti-elss",
    "uti-balanced",
    "uti-short-term",
    "uti-silver-etf-fof",
)
GROWW_URLS = {
    "uti-nifty": "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth",
    "uti-small-cap": "https://groww.in/mutual-funds/uti-small-cap-fund-direct-growth",
    "uti-large-mid-cap": "https://groww.in/mutual-funds/uti-large-mid-cap-fund-direct-growth",
    "uti-equity": "https://groww.in/mutual-funds/uti-equity-fund-direct-growth",
    "uti-mastershare": "https://groww.in/mutual-funds/uti-unit-scheme-1986-mastershare-direct-growth",
    "uti-mid-cap": "https://groww.in/mutual-funds/uti-mid-cap-fund-direct-growth",
    "uti-elss": "https://groww.in/mutual-funds/uti-long-term-equity-fund-tax-saving-direct-growth",
    "uti-balanced": "https://groww.in/mutual-funds/uti-balanced-fund-direct-growth",
    "uti-short-term": "https://groww.in/mutual-funds/uti-short-term-direct-growth",
    "uti-silver-etf-fof": "https://groww.in/mutual-funds/uti-silver-etf-fof-direct-growth",
}
DOC_TYPES = {
    "factsheet",
    "kim",
    "sid",
    "amc_faq",
    "statement_guide",
    "tax_guide",
    "amfi",
    "sebi",
}


def test_registry_has_the_ten_schemes():
    corpus = load_corpus()
    assert corpus.scheme_count == 10
    assert tuple(item.scheme_id for item in corpus.schemes) == EXPECTED_IDS
    for scheme in corpus.schemes:
        assert scheme.name
        assert scheme.aliases
        assert scheme.category
        assert scheme.groww_url == GROWW_URLS[scheme.scheme_id]
        assert scheme.factsheet_url.startswith("https://www.utimf.com/")
        assert scheme.primary_url.startswith("https://www.utimf.com/")
    assert corpus.ambiguous_aliases == {}


def test_manifest_is_allowlisted_and_in_range():
    corpus = load_corpus()
    assert 15 <= corpus.document_count <= 25
    types = {item.doc_type for item in corpus.documents}
    assert types <= DOC_TYPES
    assert {"statement_guide", "amfi", "sebi"} <= types
    hosts = set()
    for document in corpus.documents:
        assert "groww.in" not in document.url
        hosts.add(document.url.split("/")[2].split("?")[0])
    assert hosts <= {
        "www.utimf.com",
        "utimf.com",
        "www.amfiindia.com",
        "amfiindia.com",
        "www.sebi.gov.in",
        "sebi.gov.in",
        "investor.sebi.gov.in",
    }
    covered = {
        document.scheme_id
        for document in corpus.documents
        if document.doc_type in {"factsheet", "kim", "sid"} and document.scheme_id
    }
    assert covered == set(EXPECTED_IDS)
    assert corpus.amfi_education_url in {item.url for item in corpus.documents}
    assert corpus.sebi_education_url in {item.url for item in corpus.documents}
    assert "amfiindia.com" in corpus.amfi_education_url
    assert "sebi.gov.in" in corpus.sebi_education_url
    assert corpus.factsheet_index_url() == "https://www.utimf.com/downloads/kim"


def test_alias_match_is_whole_phrase_and_prefers_the_longer_name():
    corpus = load_corpus()
    assert corpus.schemes_named_in("What is the exit load of UTI Nifty Fund?") == ("uti-nifty",)
    assert corpus.schemes_named_in("nifty 50 index fund") == ("uti-nifty",)
    assert corpus.schemes_named_in("large and mid cap") == ("uti-large-mid-cap",)
    assert corpus.schemes_named_in("mid cap") == ("uti-mid-cap",)
    assert corpus.schemes_named_in(
        "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth"
    ) == ("uti-nifty",)
    assert corpus.schemes_named_in("UTII Nifty") == ()


def test_shared_alias_is_flagged_for_two_schemes(tmp_path: Path):
    first = _scheme("uti-nifty")
    second = _scheme("uti-mid-cap")
    first["aliases"] = ["shared label"]
    second["aliases"] = ["shared label"]
    _write(
        tmp_path,
        schemes=[first, second],
        documents=[_document("https://www.utimf.com/nifty")],
    )
    corpus = load_corpus(tmp_path)
    assert corpus.ambiguous_aliases["shared label"] == ("uti-mid-cap", "uti-nifty")
    assert "shared label" not in corpus.alias_index
    assert set(corpus.schemes_named_in("shared label")) == {"uti-nifty", "uti-mid-cap"}


@pytest.mark.parametrize(
    "url",
    [
        "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth",
        "https://www.utimf.com:8443/factsheet",
        "https://user@www.utimf.com/factsheet",
        "https://127.0.0.1/factsheet",
        "file:///tmp/factsheet.pdf",
        "javascript:alert(1)",
        "www.utimf.com/factsheet",
    ],
)
def test_loader_rejects_a_bad_manifest_url(tmp_path: Path, url: str):
    _write(
        tmp_path,
        schemes=[_scheme("uti-nifty")],
        documents=[_document(url), _document("https://www.amfiindia.com/edu", doc_type="amfi", scheme_id=None)],
    )
    with pytest.raises(CorpusLoadError):
        load_corpus(tmp_path)


def test_registry_rejects_a_groww_factsheet(tmp_path: Path):
    scheme = _scheme("uti-nifty")
    scheme["factsheet_url"] = "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth"
    _write(tmp_path, schemes=[scheme], documents=[_document("https://www.utimf.com/nifty")])
    with pytest.raises(CorpusLoadError, match="allowlist"):
        load_corpus(tmp_path)


def test_registry_rejects_a_groww_primary_url(tmp_path: Path):
    scheme = _scheme("uti-nifty")
    scheme["primary_url"] = "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth"
    _write(tmp_path, schemes=[scheme], documents=[_document("https://www.utimf.com/nifty")])
    with pytest.raises(CorpusLoadError, match="allowlist"):
        load_corpus(tmp_path)


def test_manifest_rejects_an_unknown_scheme_id(tmp_path: Path):
    _write(
        tmp_path,
        schemes=[_scheme("uti-nifty")],
        documents=[_document("https://www.utimf.com/other", scheme_id="uti-other")],
    )
    with pytest.raises(CorpusLoadError, match="uti-other"):
        load_corpus(tmp_path)


def test_null_scheme_id_is_a_shared_document(tmp_path: Path):
    _write(
        tmp_path,
        schemes=[_scheme("uti-nifty")],
        documents=[_document("https://www.amfiindia.com/edu", doc_type="amfi", scheme_id=None)],
    )
    corpus = load_corpus(tmp_path)
    assert corpus.documents[0].scheme_id is None


def test_education_url_must_be_in_the_manifest(tmp_path: Path):
    _write(
        tmp_path,
        schemes=[_scheme("uti-nifty")],
        documents=[_document("https://www.utimf.com/nifty")],
        amfi_url="https://www.amfiindia.com/not-in-the-manifest",
        link_education=False,
    )
    with pytest.raises(CorpusLoadError, match="missing from the manifest"):
        load_corpus(tmp_path)


def _scheme(scheme_id: str, alias: str = "nifty fund") -> dict:
    return {
        "scheme_id": scheme_id,
        "name": f"Name {scheme_id}",
        "aliases": [alias],
        "category": "Index",
        "groww_url": "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth",
        "factsheet_url": "https://www.utimf.com/mutual-funds/uti-nifty-50-index-fund",
        "primary_url": "https://www.utimf.com/mutual-funds/uti-nifty-50-index-fund",
    }


def _document(url: str, doc_type: str = "factsheet", scheme_id: str | None = "uti-nifty") -> dict:
    return {
        "url": url,
        "scheme_id": scheme_id,
        "doc_type": doc_type,
        "title": "Fixture",
    }


def _write(
    folder: Path,
    schemes: list[dict],
    documents: list[dict],
    amfi_url: str = "https://www.amfiindia.com/edu",
    sebi_url: str = "https://investor.sebi.gov.in/understanding_mf.html",
    link_education: bool = True,
) -> None:
    if link_education:
        urls = {item["url"] for item in documents}
        if amfi_url not in urls:
            documents = [*documents, _document(amfi_url, doc_type="amfi", scheme_id=None)]
        if sebi_url not in urls:
            documents = [*documents, _document(sebi_url, doc_type="sebi", scheme_id=None)]
    (folder / "schemes.yaml").write_text(yaml.safe_dump(schemes), encoding="utf-8")
    (folder / "manifest.yaml").write_text(yaml.safe_dump(documents), encoding="utf-8")
    (folder / "education.yaml").write_text(
        yaml.safe_dump({"amfi_url": amfi_url, "sebi_url": sebi_url}),
        encoding="utf-8",
    )

"""Fixed replies. Refusals and redirects are written here, not by a model."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.guard.intent import GuardResult
from app.schemes import Corpus, check_official_url

_URL = re.compile(r"https?://\S+")
_FOOTER = re.compile(r"^Last updated from sources:.*$", re.MULTILINE)
# A period in these abbreviations is not the end of a sentence.
_ABBREV = re.compile(r"\b(?:Rs|e\.g|i\.e)\.", re.IGNORECASE)
_DOT = "\u0001"


@dataclass(frozen=True)
class TemplateReply:
    kind: str
    intent: str
    text: str
    citation_url: str
    citation_title: str
    doc_type: str
    footer: str

    @property
    def full_text(self) -> str:
        return f"{self.text}\n{self.footer}"


def footer_line(display_date: str) -> str:
    """Footer from the cited document's display date."""
    date = " ".join((display_date or "").split())
    if not date or "http://" in date or "https://" in date:
        raise ValueError("display date must be a plain date")
    return f"Last updated from sources: {date}"


def split_sentences(text: str) -> list[str]:
    """Sentences in the reply body. The footer, the citation URL, and Rs. / e.g. / i.e. do not split."""
    body = _FOOTER.sub("", text or "")
    body = _URL.sub("", body).strip()
    if not body:
        return []
    protected = _ABBREV.sub(lambda match: match.group(0).replace(".", _DOT), body)
    sentences: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+", protected):
        cleaned = part.replace(_DOT, ".").strip()
        if cleaned:
            sentences.append(cleaned)
    return sentences


def sentence_count(text: str) -> int:
    """How many sentences `split_sentences` keeps."""
    return len(split_sentences(text))


def reply_for(result: GuardResult, corpus: Corpus, display_date: str) -> TemplateReply | None:
    """Template for a non-factual class. A factual result has no template."""
    if result.intent == "factual":
        return None
    if result.intent == "advice":
        return advice_reply(corpus, display_date)
    if result.intent == "comparison":
        return comparison_reply(corpus, display_date)
    if result.intent == "pii":
        return pii_reply(corpus, display_date)
    if result.intent == "out_of_scope":
        return out_of_scope_reply(corpus, display_date)
    if result.intent == "performance":
        return performance_reply(corpus, result.scheme_id, display_date)
    if result.intent == "clarify_scheme":
        return clarify_reply(corpus, display_date)
    raise ValueError(f"no template for {result.intent}")


def advice_reply(corpus: Corpus, display_date: str) -> TemplateReply:
    body = (
        "This assistant shares facts from official UTI, AMFI, and SEBI documents and does not "
        "provide investment advice or recommendations. Investor education material is published by AMFI."
    )
    return _cite(corpus, "advice", "refusal", body, corpus.amfi_education_url, display_date)


def comparison_reply(corpus: Corpus, display_date: str) -> TemplateReply:
    body = (
        "This assistant does not compare schemes or say which fund is better. "
        "Investor education material is published by AMFI."
    )
    return _cite(corpus, "comparison", "refusal", body, corpus.amfi_education_url, display_date)


def pii_reply(corpus: Corpus, display_date: str) -> TemplateReply:
    body = (
        "This assistant does not take personal identifiers such as PAN, Aadhaar, account numbers, "
        "OTPs, email, or phone numbers. It only shares facts from official documents."
    )
    return _cite(corpus, "pii", "refusal", body, corpus.sebi_education_url, display_date)


def out_of_scope_reply(corpus: Corpus, display_date: str) -> TemplateReply:
    body = (
        "This assistant covers these ten UTI Mutual Fund schemes and does not answer questions "
        "about other funds, stocks, or IPOs. Investor education material is published by AMFI."
    )
    return _cite(corpus, "out_of_scope", "refusal", body, corpus.amfi_education_url, display_date)


def performance_reply(
    corpus: Corpus,
    scheme_id: str | None,
    display_date: str,
) -> TemplateReply:
    if scheme_id is None:
        body = (
            "This assistant does not calculate or quote returns, CAGR, or rankings. "
            "Which of the ten UTI schemes should the factsheet be for?"
        )
        return _cite(
            corpus,
            "performance",
            "performance_redirect",
            body,
            corpus.factsheet_index_url(),
            display_date,
        )
    scheme = corpus.scheme(scheme_id)
    body = (
        "This assistant does not calculate or quote returns, CAGR, or rankings. "
        f"Published performance for {scheme.name} is on the official factsheet."
    )
    return _cite(corpus, "performance", "performance_redirect", body, scheme.factsheet_url, display_date)


def clarify_reply(corpus: Corpus, display_date: str) -> TemplateReply:
    body = "Which of the ten UTI schemes is this question about?"
    return _cite(corpus, "clarify_scheme", "clarify_scheme", body, corpus.factsheet_index_url(), display_date)


def not_in_corpus_reply(corpus: Corpus, scheme_id: str, display_date: str) -> TemplateReply:
    """Missing fact. The wording does not fill in a number from memory."""
    scheme = corpus.scheme(scheme_id)
    body = (
        f"The indexed official documents do not contain that fact for {scheme.name}. "
        "The official page for this scheme is listed here."
    )
    return _cite(corpus, "not_in_corpus", "not_in_corpus", body, scheme.primary_url, display_date)


def missing_reply(
    corpus: Corpus,
    scheme_id: str | None,
    display_date: str,
    field: str | None = None,
) -> TemplateReply:
    """Not-in-corpus. A known scheme cites `primary_url`. A shared question cites one official page."""
    if scheme_id:
        return not_in_corpus_reply(corpus, scheme_id, display_date)
    if field == "download":
        url = _first_doc(corpus, {"statement_guide", "tax_guide"}) or corpus.factsheet_index_url()
        body = (
            "The indexed official documents do not contain that fact. "
            "The official statement page is listed here."
        )
        return _cite(corpus, "not_in_corpus", "not_in_corpus", body, url, display_date)
    body = (
        "The indexed official documents do not contain that fact. "
        "Which of the ten UTI schemes is this question about?"
    )
    return _cite(corpus, "not_in_corpus", "not_in_corpus", body, corpus.factsheet_index_url(), display_date)


def _first_doc(corpus: Corpus, doc_types: set[str]) -> str | None:
    for document in corpus.documents:
        if document.doc_type in doc_types:
            return document.url
    return None


def _cite(
    corpus: Corpus,
    intent: str,
    kind: str,
    body: str,
    url: str,
    display_date: str,
) -> TemplateReply:
    citation = check_official_url(url)
    if _URL.search(body):
        raise ValueError("template body must not contain its own URL")
    title, doc_type = source_for(corpus, citation)
    text = f"{body} {citation}"
    return TemplateReply(
        kind=kind,
        intent=intent,
        text=text,
        citation_url=citation,
        citation_title=title,
        doc_type=doc_type,
        footer=footer_line(display_date),
    )


def source_for(corpus: Corpus, url: str) -> tuple[str, str]:
    for document in corpus.documents:
        if document.url == url:
            return document.title, document.doc_type
    for scheme in corpus.schemes:
        if url in {scheme.factsheet_url, scheme.primary_url}:
            return scheme.name, "factsheet"
    return "Official document", "factsheet"

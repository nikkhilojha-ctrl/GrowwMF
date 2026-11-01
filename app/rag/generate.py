"""Quote a fact span, or compress retrieved chunks.

The Groq client is built only in the grounded path, and only when
`GROQ_API_KEY` is set. Refusals never reach this module's chat call.
A rejected model reply is dropped. The citation is always the retrieved URL.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from app.db import display_date
from app.rag.retrieve import RetrievalResult
from app.rag.templates import (
    footer_line,
    missing_reply,
    pii_reply,
    source_for,
    split_sentences,
)
from app.rag.validate import ReplyDraft, plain_numbers, validate
from app.schemes import Corpus, Scheme
from app.settings import Settings

log = logging.getLogger(__name__)

FIELD_LABELS = {
    "expense_ratio": "expense ratio",
    "exit_load": "exit load",
    "minimum_sip": "minimum SIP",
    "minimum_lumpsum": "minimum lump sum",
    "benchmark": "benchmark",
    "riskometer": "riskometer",
    "lock_in": "lock-in period",
}
_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_FOOTER_LINE = re.compile(r"^Last updated from sources:\s*\S", re.IGNORECASE)


@dataclass(frozen=True)
class Answer:
    kind: str
    text: str
    citation_url: str
    citation_title: str
    doc_type: str
    footer: str
    scheme_id: str | None


@dataclass(frozen=True)
class _Unsupported:
    """The model said the passages do not contain the answer."""


UNSUPPORTED = _Unsupported()


def answer_from_retrieval(
    question: str,
    scheme: Scheme | None,
    field: str | None,
    retrieval: RetrievalResult,
    corpus: Corpus,
    settings: Settings,
    missing_date: str,
    pii_date: str | None = None,
) -> Answer:
    """Fact row, else grounded prose when a key is set, else a short extract from the chunk."""
    scheme_id = None if scheme is None else scheme.scheme_id
    if retrieval.fact is not None:
        draft = _fact_draft(retrieval, corpus)
        return _accept(draft, corpus, scheme_id, field, missing_date, pii_date)
    if retrieval.empty or not retrieval.chunks:
        return _from_template(missing_reply(corpus, scheme_id, missing_date, field), scheme_id)
    grounded = _grounded_draft(question, scheme, retrieval, corpus, settings)
    if grounded is UNSUPPORTED:
        return _from_template(missing_reply(corpus, scheme_id, missing_date, field), scheme_id)
    if isinstance(grounded, ReplyDraft):
        checked = validate(grounded, corpus)
        if checked.ok:
            return _from_draft(grounded, "answer", scheme_id)
        if "pii" in checked.reasons:
            return _from_template(pii_reply(corpus, pii_date or missing_date), scheme_id)
    excerpt = _excerpt_draft(retrieval, corpus)
    if excerpt is not None and validate(excerpt, corpus).ok:
        return _from_draft(excerpt, "answer", scheme_id)
    return _from_template(missing_reply(corpus, scheme_id, missing_date, field), scheme_id)


def chat_request(
    question: str,
    scheme_name: str | None,
    chunks: tuple,
    settings: Settings,
) -> dict:
    """Groq payload. Temperature is 0. The messages are this question and at most four chunks."""
    return {
        "model": settings.groq_model,
        "temperature": 0,
        "messages": build_messages(question, scheme_name, chunks),
    }


def build_messages(question: str, scheme_name: str | None, chunks: tuple) -> list[dict[str, str]]:
    """System instructions plus one user turn. No earlier answer is included."""
    passages: list[str] = []
    for index, chunk in enumerate(tuple(chunks)[:4], start=1):
        title = getattr(chunk, "title", "") or "Official document"
        text = getattr(chunk, "text", "") or ""
        passages.append(f"[{index}] {title}\n{text}")
    scheme_line = scheme_name or "None"
    user = (
        f"Scheme: {scheme_line}\n"
        f"Question: {question}\n"
        "Passages:\n"
        + "\n\n".join(passages)
    )
    system = (
        "You compress official mutual-fund passages into facts. "
        'Return JSON {"sentences": ["..."], "supported": true}. '
        "Use at most 3 sentences and only these passages. Copy numbers exactly. "
        "Do not recommend, compute, compare, or include a URL. "
        "If the passages do not contain the answer, return "
        '{"sentences": [], "supported": false}.'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def complete_chat(settings: Settings, body: dict) -> str | None:
    """POST `{GROQ_BASE_URL}/chat/completions`. Returns None on 4xx, 5xx, or timeout.

    The HTTP client is created here, and only when the API key is set.
    The key is sent as a bearer token and is not logged.
    """
    if not settings.groq_api_key:
        return None
    url = f"{settings.groq_base_url}/chat/completions"
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("groq request failed: %s", type(exc).__name__)
        return None


def resolve(draft: ReplyDraft, corpus: Corpus, fallback: Answer) -> Answer:
    """Return the draft when it validates. Otherwise return `fallback` and drop the draft text."""
    if validate(draft, corpus).ok:
        return _from_draft(draft, "answer", fallback.scheme_id)
    return fallback


def _accept(
    draft: ReplyDraft,
    corpus: Corpus,
    scheme_id: str | None,
    field: str | None,
    missing_date: str,
    pii_date: str | None,
) -> Answer:
    checked = validate(draft, corpus)
    if checked.ok:
        return _from_draft(draft, "answer", scheme_id)
    if "pii" in checked.reasons:
        return _from_template(pii_reply(corpus, pii_date or missing_date), scheme_id)
    return _from_template(missing_reply(corpus, scheme_id, missing_date, field), scheme_id)


def _fact_draft(retrieval: RetrievalResult, corpus: Corpus) -> ReplyDraft:
    fact = retrieval.fact
    assert fact is not None
    title, doc_type = _labels(corpus, fact.source_url, fact.title, fact.doc_type)
    body = _fact_body(fact.scheme_id, fact.field, fact.value_text, title, doc_type, corpus)
    shown = display_date(fact.retrieved_at)
    return _draft(
        body=body,
        citation_url=fact.source_url,
        citation_title=title,
        doc_type=doc_type,
        cited_text=fact.value_text,
        scheme_name=corpus.scheme(fact.scheme_id).name,
        display_date=shown,
    )


def _fact_body(
    scheme_id: str,
    field: str,
    value_text: str,
    title: str,
    doc_type: str,
    corpus: Corpus,
) -> str:
    scheme_name = corpus.scheme(scheme_id).name
    label = FIELD_LABELS.get(field, "stated fact")
    first = f"The {label} stated for {scheme_name} is {_quoted(value_text.strip())}"
    second = _document_sentence(title, doc_type, f"{value_text} {scheme_name}")
    return f"{first} {second}"


def _excerpt_draft(retrieval: RetrievalResult, corpus: Corpus) -> ReplyDraft | None:
    chunk = retrieval.chunks[0]
    sentences = split_sentences(chunk.text)[:2]
    body = " ".join(sentences).strip()
    body = _URL.sub(" ", body)
    body = " ".join(body.split())
    if not body:
        return None
    title, doc_type = _labels(corpus, chunk.url, chunk.title, chunk.doc_type)
    scheme_name = ""
    if chunk.scheme_id:
        scheme_name = corpus.scheme(chunk.scheme_id).name
    return _draft(
        body=body,
        citation_url=chunk.url,
        citation_title=title,
        doc_type=doc_type,
        cited_text=chunk.text,
        scheme_name=scheme_name,
        display_date=display_date(chunk.retrieved_at),
    )


def _grounded_draft(
    question: str,
    scheme: Scheme | None,
    retrieval: RetrievalResult,
    corpus: Corpus,
    settings: Settings,
):
    if not settings.chat_enabled:
        return None
    scheme_name = None if scheme is None else scheme.name
    raw = complete_chat(settings, chat_request(question, scheme_name, retrieval.chunks, settings))
    if raw is None:
        return None
    parsed = _parse_model(raw)
    if not isinstance(parsed, list):
        return UNSUPPORTED
    chunk = retrieval.chunks[0]
    title, doc_type = _labels(corpus, chunk.url, chunk.title, chunk.doc_type)
    return _draft(
        body=" ".join(parsed),
        citation_url=chunk.url,
        citation_title=title,
        doc_type=doc_type,
        cited_text=chunk.text,
        scheme_name=scheme_name or "",
        display_date=display_date(chunk.retrieved_at),
    )


def _parse_model(raw: str) -> list[str] | _Unsupported | None:
    data = _json_object(raw)
    if data is None or "sentences" not in data or "supported" not in data:
        return None
    if data["supported"] is False:
        return UNSUPPORTED
    if data["supported"] is not True or not isinstance(data["sentences"], list):
        return None
    sentences: list[str] = []
    for item in data["sentences"]:
        if not isinstance(item, str):
            return None
        cleaned = " ".join(item.split())
        if not cleaned or _FOOTER_LINE.match(cleaned):
            continue
        sentences.append(cleaned)
    if not sentences:
        return UNSUPPORTED
    return sentences


def _json_object(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return data


def _draft(
    *,
    body: str,
    citation_url: str,
    citation_title: str,
    doc_type: str,
    cited_text: str,
    scheme_name: str,
    display_date: str,
) -> ReplyDraft:
    text = _attach_citation(body, citation_url)
    return ReplyDraft(
        text=text,
        citation_url=citation_url,
        citation_title=citation_title,
        doc_type=doc_type,
        footer=footer_line(display_date),
        cited_text=cited_text,
        scheme_name=scheme_name,
        display_date=display_date,
    )


def _attach_citation(body: str, citation_url: str) -> str:
    """Keep a model URL in the text so the validator can reject it. Add ours only when there is none."""
    found = [match.group(0).rstrip(".,);]") for match in _URL.finditer(body)]
    if not found:
        return f"{body} {citation_url}"
    return body


def _labels(corpus: Corpus, url: str, title: str, doc_type: str) -> tuple[str, str]:
    found_title, found_type = source_for(corpus, url)
    return (title or found_title, doc_type or found_type)


def _document_sentence(title: str, doc_type: str, allowed: str) -> str:
    cleaned = " ".join((title or "").split())
    if cleaned and not _title_adds_numbers(cleaned, allowed):
        return f"This wording is from the {cleaned}."
    label = (doc_type or "document").replace("_", " ")
    if label in {"factsheet", "kim", "sid"}:
        return f"This wording is from the scheme {label}."
    return "This wording is from the official document."


def _title_adds_numbers(title: str, allowed: str) -> bool:
    allowed_numbers = plain_numbers(allowed)
    return any(number not in allowed_numbers for number in plain_numbers(title))


def _quoted(value: str) -> str:
    if "'" in value and '"' not in value:
        return f'"{value}".'
    return f"'{value}'."


def _from_draft(draft: ReplyDraft, kind: str, scheme_id: str | None) -> Answer:
    return Answer(
        kind=kind,
        text=draft.text,
        citation_url=draft.citation_url,
        citation_title=draft.citation_title,
        doc_type=draft.doc_type,
        footer=draft.footer,
        scheme_id=scheme_id,
    )


def _from_template(reply, scheme_id: str | None) -> Answer:
    return Answer(
        kind=reply.kind,
        text=reply.text,
        citation_url=reply.citation_url,
        citation_title=reply.citation_title,
        doc_type=reply.doc_type,
        footer=reply.footer,
        scheme_id=scheme_id,
    )

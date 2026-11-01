"""Reject a reply that breaks the response contract.

A failed check discards the text. The caller substitutes an extractive
answer or the not-in-corpus template and does not return the rejected text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.guard.pii import contains_pii
from app.rag.templates import footer_line, sentence_count
from app.schemes import Corpus, CorpusLoadError, check_official_url

_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_BANNED = re.compile(
    r"\b(?:buy|sell|switch|prefer\w*|recommend\w*)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
    r"(?:\s*(%|％)|\s+(percent)\b)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReplyDraft:
    """One complete reply before it is stored. `text` carries the single citation URL."""

    text: str
    citation_url: str
    citation_title: str
    doc_type: str
    footer: str
    cited_text: str
    scheme_name: str
    display_date: str


@dataclass(frozen=True)
class Validation:
    ok: bool
    reasons: tuple[str, ...]


def validate(draft: ReplyDraft, corpus: Corpus) -> Validation:
    """Architecture section 13.3. `ok` is false when any check fails."""
    reasons: list[str] = []
    count = sentence_count(f"{draft.text}\n{draft.footer}")
    if not 1 <= count <= 3:
        reasons.append("sentences")
    citation = _citation(draft.citation_url, corpus)
    if citation is None:
        reasons.append("citation_host")
    elif citation not in {document.url for document in corpus.documents}:
        reasons.append("citation_manifest")
    urls = _urls(f"{draft.text}\n{draft.footer}")
    if citation is None or urls != [citation]:
        reasons.append("citation_unity")
    if _figure_problems(draft):
        reasons.append("figures")
    if _BANNED.search(_without_urls(draft.text)):
        reasons.append("banned")
    if not _footer_ok(draft):
        reasons.append("footer")
    if contains_pii(draft.text) or contains_pii(draft.footer):
        reasons.append("pii")
    return Validation(ok=not reasons, reasons=tuple(reasons))


def replace_footer_date(footer: str, display_date: str) -> str | None:
    """Overwrite one footer date with the cited document's display date.

    A missing footer or a second footer line is not repaired. The caller
    treats that as a failed check and substitutes a template.
    """
    line = " ".join((footer or "").split())
    if not line.startswith("Last updated from sources:"):
        return None
    if line.count("Last updated from sources:") != 1:
        return None
    return footer_line(display_date)


def plain_numbers(text: str) -> set[str]:
    """Digit sequences that are not written as a percent."""
    return {canon for canon, is_percent in _numbers(text) if not is_percent}


def _citation(url: str, corpus: Corpus) -> str | None:
    try:
        return check_official_url(url)
    except CorpusLoadError:
        return None


def _footer_ok(draft: ReplyDraft) -> bool:
    try:
        expected = footer_line(draft.display_date)
    except ValueError:
        return False
    if draft.footer != expected:
        return False
    body_lines = [line.strip() for line in draft.text.splitlines() if line.strip()]
    return not any(line.startswith("Last updated from sources:") for line in body_lines)


def _figure_problems(draft: ReplyDraft) -> bool:
    """Percents must appear in the cited text. Other numbers may also be the scheme name."""
    answer = _without_urls(draft.text)
    cited = {canon for canon, _percent in _numbers(draft.cited_text)}
    named = plain_numbers(draft.scheme_name)
    for canon, is_percent in _numbers(answer):
        if is_percent:
            if canon not in cited:
                return True
            continue
        if canon not in cited and canon not in named:
            return True
    return False


def _numbers(text: str) -> list[tuple[str, bool]]:
    found: list[tuple[str, bool]] = []
    for match in _NUMBER.finditer(text or ""):
        whole, decimal, symbol, word = match.groups()
        digits = whole.replace(",", "")
        canon = digits if decimal is None else f"{digits}.{decimal}"
        found.append((canon, bool(symbol or word)))
    return found


def _without_urls(text: str) -> str:
    return _URL.sub(" ", text or "")


def _urls(text: str) -> list[str]:
    return [match.group(0).rstrip(".,);]") for match in _URL.finditer(text or "")]

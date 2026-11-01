"""Copy labeled fact spans. A missing span is skipped, never filled in."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingest.chunk import ChunkDraft

FACT_FIELDS = (
    "expense_ratio",
    "exit_load",
    "minimum_sip",
    "minimum_lumpsum",
    "benchmark",
    "riskometer",
    "lock_in",
)

_LOCK_IN = re.compile(r"(?:no\s+|without\s+)?lock[\s-]*in[^\n]*", re.IGNORECASE)


@dataclass(frozen=True)
class Fact:
    field: str
    value_text: str
    page: int | None


def field_for_heading(heading: str | None) -> str | None:
    if not heading or not heading.strip():
        return None
    text = heading.strip()
    if re.search(r"riskometer", text, re.IGNORECASE) and re.search(
        r"benchmark", text, re.IGNORECASE
    ):
        return None
    if re.search(r"expense\s+ratio|total\s+expense|\bdaily\s+ter\b|\bter\b", text, re.IGNORECASE):
        return "expense_ratio"
    if re.search(r"exit\s+load", text, re.IGNORECASE):
        return "exit_load"
    if re.search(r"minimum\s+sip|min\.?\s+sip|sip\s+minimum", text, re.IGNORECASE):
        return "minimum_sip"
    if re.search(
        r"minimum\s+lump\s*sum|lump\s*sum\s+minimum|minimum\s+investment|min\.?\s+investment",
        text,
        re.IGNORECASE,
    ):
        return "minimum_lumpsum"
    if re.search(r"lock[\s-]*in", text, re.IGNORECASE):
        return "lock_in"
    if re.search(r"riskometer", text, re.IGNORECASE):
        return "riskometer"
    if re.search(r"benchmark", text, re.IGNORECASE):
        return "benchmark"
    return None


def extract_facts(chunks: list[ChunkDraft], scheme_id: str | None) -> list[Fact]:
    """One fact per field. Shared documents (null scheme id) produce none."""
    if not scheme_id:
        return []
    found: dict[str, Fact] = {}
    for chunk in chunks:
        for fact in _facts_in_chunk(chunk):
            found.setdefault(fact.field, fact)
    return [found[field] for field in FACT_FIELDS if field in found]


def _facts_in_chunk(chunk: ChunkDraft) -> list[Fact]:
    field = field_for_heading(chunk.heading)
    if field is None:
        return []
    value = _clean_value(_strip_heading(chunk.text, chunk.heading or ""))
    facts: list[Fact] = []
    if value is not None and _value_fits(field, value):
        facts.append(Fact(field=field, value_text=value, page=chunk.page))
    if field == "exit_load" and value is not None:
        span = _lock_in_span(value)
        if span is not None:
            facts.append(Fact(field="lock_in", value_text=span, page=chunk.page))
    return facts


def _strip_heading(text: str, heading: str) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    head = heading.strip()
    if first.lower() == head.lower():
        return "\n".join(lines[1:]).strip()
    if first.lower().startswith(head.lower()):
        rest = first[len(head) :].lstrip(" :-–—")
        tail = "\n".join(lines[1:]).strip()
        return "\n".join(part for part in (rest, tail) if part)
    return text.strip()


def _clean_value(value: str) -> str | None:
    collapsed = re.sub(r"[ \t]+", " ", value).strip()
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    if not collapsed:
        return None
    if re.fullmatch(r"[\s%\-:()]+", collapsed):
        return None
    if re.fullmatch(r"%?\s*as on\s*\(?\s*\)?", collapsed, re.IGNORECASE):
        return None
    return collapsed


def _value_fits(field: str, value: str) -> bool:
    if len(value) > 1500:
        return False
    if field == "expense_ratio" and not re.search(r"\d", value):
        return False
    if field in {"minimum_sip", "minimum_lumpsum"} and not re.search(r"\d", value):
        return False
    if field == "benchmark" and re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", value):
        return False
    return True


def _lock_in_span(value: str) -> str | None:
    """The lock-in wording already in the exit-load span, or nothing."""
    match = _LOCK_IN.search(value)
    if match is None:
        return None
    span = match.group(0).strip(" \t.;")
    if span.endswith(")") and span.count("(") < span.count(")"):
        span = span[:-1].rstrip()
    return span or None

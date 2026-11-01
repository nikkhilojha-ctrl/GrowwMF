"""Split parsed blocks into retrieval chunks.

Tokens are whitespace-separated words. A block is about 800 tokens with 120
tokens of overlap. A fact heading stays one chunk so the label and the value
are stored together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_SIZE = 800
TOKEN_OVERLAP = 120

# Headings that must not be split from the value that follows them.
_FACT_HEADING = re.compile(
    r"^(?:"
    r"exit\s+load"
    r"|expense\s+ratio"
    r"|total\s+expense\s+ratio"
    r"|daily\s+ter(?:\s*\(\s*%\s*\))?"
    r"|benchmark(?:\s+index)?"
    r"|riskometer"
    r"|lock[\s-]*in(?:\s+period)?"
    r"|minimum\s+sip"
    r"|minimum\s+lump\s*sum"
    r"|minimum\s+investment"
    r"|systematic\s+investment\s+plan"
    r"|sip"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Block:
    heading: str | None
    text: str
    page: int | None


@dataclass(frozen=True)
class ChunkDraft:
    heading: str | None
    text: str
    page: int | None


def match_fact_heading(line: str) -> tuple[str, str] | None:
    """Return the heading and any same-line value when `line` is a fact label."""
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return None
    match = _FACT_HEADING.match(stripped)
    if match is None:
        return None
    heading = match.group(0).strip()
    rest = stripped[match.end() :].lstrip(" :-–—")
    combined = f"{heading} {rest}"
    if re.search(r"riskometer", combined, re.IGNORECASE) and re.search(
        r"benchmark", combined, re.IGNORECASE
    ):
        return None
    return heading, rest


def is_protected_heading(heading: str | None) -> bool:
    if not heading:
        return False
    return match_fact_heading(heading) is not None


def chunk_blocks(
    blocks: list[Block],
    size: int = TOKEN_SIZE,
    overlap: int = TOKEN_OVERLAP,
) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if is_protected_heading(block.heading) or len(text.split()) <= size:
            drafts.append(ChunkDraft(heading=block.heading, text=text, page=block.page))
            continue
        for piece in _windows(text, size, overlap):
            drafts.append(ChunkDraft(heading=block.heading, text=piece, page=block.page))
    return drafts


def _windows(text: str, size: int, overlap: int) -> list[str]:
    tokens = text.split()
    if not tokens:
        return []
    if overlap >= size:
        overlap = max(0, size // 5)
    step = max(1, size - overlap)
    windows: list[str] = []
    start = 0
    while start < len(tokens):
        windows.append(" ".join(tokens[start : start + size]))
        if start + size >= len(tokens):
            break
        start += step
    return windows

"""HTML via Trafilatura and PDF page text. Empty PDF pages are dropped."""

from __future__ import annotations

import json
import re
from html import unescape
from io import BytesIO

import trafilatura
from lxml import etree, html
from pypdf import PdfReader

from app.ingest.chunk import Block, match_fact_heading
from app.ingest.extract_facts import field_for_heading

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_LABELISH = re.compile(r"^[A-Z0-9][^.]{0,80}$")


def parse_document(body: bytes, content_type: str, url: str) -> list[Block]:
    if _is_pdf(body, content_type, url):
        return parse_pdf(body)
    return parse_html(body, content_type)


def parse_html(body: bytes, content_type: str) -> list[Block]:
    html_text = _decode(body, content_type)
    blocks: list[Block] = []
    document = _parse_tree(html_text)
    if document is not None:
        blocks.extend(_heading_sections(document))
        blocks.extend(_class_labels(document))
    blocks.extend(_embedded_state(html_text, blocks))
    prose = _article_text(html_text)
    if prose:
        blocks.extend(split_text(prose, page=None))
    return _dedupe(blocks)


def parse_pdf(body: bytes) -> list[Block]:
    reader = PdfReader(BytesIO(body))
    blocks: list[Block] = []
    for number, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        if not raw.strip():
            continue
        blocks.extend(split_text(raw, page=number))
    return _dedupe(blocks)


def split_text(text: str, page: int | None) -> list[Block]:
    """Pull fact headings out of plain text and leave the rest as prose."""
    lines = [line.strip() for line in text.splitlines()]
    blocks: list[Block] = []
    prose: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            if prose and prose[-1] != "":
                prose.append("")
            index += 1
            continue
        matched = match_fact_heading(line)
        if matched is None:
            prose.append(line)
            index += 1
            continue
        _flush_prose(blocks, prose, page)
        prose = []
        heading, rest = matched
        value_lines = [rest] if rest else []
        index += 1
        while index < len(lines) and lines[index]:
            if match_fact_heading(lines[index]) is not None:
                break
            if value_lines and _looks_like_label(lines[index]):
                break
            value_lines.append(lines[index])
            index += 1
            if len(value_lines) >= 8:
                break
        value = "\n".join(part for part in value_lines if part).strip()
        if _usable(value):
            blocks.append(Block(heading=heading, text=f"{heading}\n{value}", page=page))
    _flush_prose(blocks, prose, page)
    return blocks


def _heading_sections(document: html.HtmlElement) -> list[Block]:
    blocks: list[Block] = []
    for node in document.xpath("//h1|//h2|//h3|//h4|//h5|//h6"):
        label = _text_of(node)
        if field_for_heading(label) is None:
            continue
        value = _section_after(node)
        if not _usable(value):
            continue
        blocks.append(Block(heading=label, text=f"{label}\n{value}", page=None))
    return blocks


def _class_labels(document: html.HtmlElement) -> list[Block]:
    blocks: list[Block] = []
    for node in document.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' fund-label ')]"
    ):
        blocks.extend(_pair(node, _text_of(_next_element(node))))
    for node in document.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' nav-label ')]"
    ):
        parent = node.getparent()
        value = ""
        if parent is not None and isinstance(parent.tag, str):
            values = parent.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' label-1 ')]"
            )
            if values:
                value = _text_of(values[0])
        blocks.extend(_pair(node, value))
    return blocks


def _pair(label_node: html.HtmlElement, value: str) -> list[Block]:
    label = _text_of(label_node)
    if field_for_heading(label) is None or not _usable(value):
        return []
    return [Block(heading=label, text=f"{label}\n{value.strip()}", page=None)]


def _embedded_state(html_text: str, existing: list[Block]) -> list[Block]:
    """Riskometer is published in the page state, not as a visible heading."""
    have = {field_for_heading(block.heading) for block in existing}
    record = _scheme_record(html_text)
    if record is None:
        return []
    blocks: list[Block] = []
    if "riskometer" not in have:
        risk = record.get("field_scheme_riskometer")
        if isinstance(risk, str) and _usable(_html_text(risk)) and len(risk) <= 80:
            value = _html_text(risk)
            blocks.append(Block(heading="Riskometer", text=f"Riskometer\n{value}", page=None))
    if "exit_load" not in have:
        blocks.extend(_state_field(record, "field_exit_load", "Exit Load"))
    if "benchmark" not in have:
        blocks.extend(_state_field(record, "field_bm_index", "Benchmark"))
    return blocks


def _state_field(record: dict, key: str, heading: str) -> list[Block]:
    raw = record.get(key)
    if not isinstance(raw, str):
        return []
    value = _html_text(raw)
    if not _usable(value):
        return []
    return [Block(heading=heading, text=f"{heading}\n{value}", page=None)]


def _scheme_record(html_text: str) -> dict | None:
    match = re.search(
        r'<script id="ng-state" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if (
                isinstance(node.get("field_scheme_riskometer"), str)
                and isinstance(node.get("field_fund_name"), str)
                and "field_bm_index" in node
            ):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if len(found) == 1:
        return found[0]
    title = _page_title(html_text).lower()
    if not title:
        return None
    named = [
        row
        for row in found
        if str(row.get("field_fund_name") or "").lower() in title
    ]
    if len(named) == 1:
        return named[0]
    return None


def _section_after(heading: html.HtmlElement) -> str:
    parts: list[str] = []
    node = _next_element(heading)
    while node is not None and node.tag not in _HEADING_TAGS:
        if node.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"):
            break
        text = _text_of(node)
        if text:
            parts.append(text)
        node = _next_element(node)
    return " ".join(parts).strip()


def _flush_prose(blocks: list[Block], prose: list[str], page: int | None) -> None:
    text = "\n".join(prose).strip()
    prose.clear()
    if len(text) < 40:
        return
    blocks.append(Block(heading=None, text=text, page=page))


def _looks_like_label(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 80 or "%" in stripped or " if " in stripped.lower():
        return False
    return _LABELISH.match(stripped) is not None and len(stripped.split()) <= 8


def _usable(value: str) -> bool:
    if not value or not value.strip():
        return False
    if re.fullmatch(r"[\s%\-:()]+", value.strip()):
        return False
    if re.fullmatch(r"%?\s*as on\s*\(?\s*\)?", value.strip(), re.IGNORECASE):
        return False
    return True


def _dedupe(blocks: list[Block]) -> list[Block]:
    seen: set[str] = set()
    unique: list[Block] = []
    for block in blocks:
        key = re.sub(r"\s+", " ", block.text).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(block)
    return unique


def _parse_tree(html_text: str) -> html.HtmlElement | None:
    try:
        return html.fromstring(html_text)
    except (etree.ParserError, ValueError):
        return None


def _next_element(node: html.HtmlElement) -> html.HtmlElement | None:
    nxt = node.getnext()
    while nxt is not None and not isinstance(getattr(nxt, "tag", None), str):
        nxt = nxt.getnext()
    return nxt


def _text_of(node: html.HtmlElement | None) -> str:
    if node is None or not isinstance(getattr(node, "tag", None), str):
        return ""
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        if isinstance(getattr(child, "tag", None), str):
            parts.append(_text_of(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(" ".join(parts).split())


def _html_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(fragment))
    return " ".join(text.split())


def _page_title(html_text: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())


def _article_text(html_text: str) -> str:
    """Keep the longer extract. Recall mode drops the AMFI statement page."""
    options = {"include_comments": False, "include_tables": True}
    recall = trafilatura.extract(html_text, favor_recall=True, **options) or ""
    plain = trafilatura.extract(html_text, **options) or ""
    return recall if len(recall) >= len(plain) else plain


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def _is_pdf(body: bytes, content_type: str, url: str) -> bool:
    if body.startswith(b"%PDF"):
        return True
    if "pdf" in content_type.lower():
        return True
    path = url.lower().split("?", 1)[0]
    return path.endswith(".pdf")

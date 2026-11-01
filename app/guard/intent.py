"""Rule intent and scheme resolution for one message.

Priority, strictest first: pii, advice, comparison, performance,
out_of_scope, clarify_scheme, factual. The thread scheme is an argument.
Nothing here reads a global scheme or calls a chat model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.guard.pii import contains_pii
from app.ingest.extract_facts import FACT_FIELDS
from app.schemes import ALLOWLIST_HOSTS, GROWW_HOSTS, Corpus, normalize_alias

INTENTS = (
    "pii",
    "advice",
    "comparison",
    "performance",
    "out_of_scope",
    "clarify_scheme",
    "factual",
)

_ADVICE = re.compile(
    r"\bshould\s+i\b|"
    r"\brecommend\w*\b|"
    r"\bsuitable\b|"
    r"\bgood\s+time\b|"
    r"\ballocat\w*\b|"
    r"\binvest\s+in\b|"
    r"\bworth\s+buying\b|"
    r"\bportfolio\b|"
    r"\bsafe\b|"
    r"\bbeginner\b|"
    r"\b(?:is|are|seems?|looks?)\b[\w\s,'-]{0,40}\b(?:high|low|good|bad|risky|ideal|worth)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"\bbetter\b|"
    r"\bversus\b|"
    r"\bvs\.?\b|"
    r"\bwhich\s+fund\b|"
    r"\bwhich\s+of\s+these\b|"
    r"\bwhich\s+(?:scheme|one)\b|"
    r"\bcompar\w*\b|"
    r"\bhigher\s+returns?\b|"
    r"\bdifference\s+between\b",
    re.IGNORECASE,
)
_ALL_SCHEMES = re.compile(
    r"\ball\s+of\s+them\b|"
    r"\b(?:all|every|each)\s+(?:of\s+)?(?:the\s+)?(?:scheme|fund)s?\b|"
    r"\beach\s+of\s+them\b",
    re.IGNORECASE,
)
_PERFORMANCE = re.compile(
    r"\breturns?\b|"
    r"\bcagr\b|"
    r"\bnav\b|"
    r"\balpha\b|"
    r"\brank(?:ing|ings|ed)?\b|"
    r"\bxirr\b|"
    r"\bperformance\b|"
    r"\bprofits?\b|"
    r"\byield\b|"
    r"\bhow\s+much\b.{0,48}\b(?:gain|become|grow|make|get)\b",
    re.IGNORECASE,
)
_TAX_RETURN = re.compile(r"\btax\s+returns?\b", re.IGNORECASE)
_AMC = re.compile(
    r"\b(?:hdfc|icici|sbi|axis|kotak|nippon|mirae|dsp|franklin|birla|absl|motilal|"
    r"ppfas|bandhan|edelweiss|hsbc|invesco|pgim|whiteoak|samco|zerodha|sundaram|"
    r"mahindra|quantum|navi)\b|"
    r"\bparag\s+parikh\b|"
    r"\baditya\s+birla\b|"
    r"\bcanara\s+robeco\b|"
    r"\bquant\s+(?:mutual|mf|fund)\b|"
    r"\btata\s+(?:mutual|mf|fund)\b|"
    r"\blic\s+(?:mutual|mf)\b",
    re.IGNORECASE,
)
_SCOPE = re.compile(
    r"\b(?:stocks?|shares?|ipos?|share\s+market|equity\s+shares?)\b|"
    r"\btax\s+filings?\b|"
    r"\bfile\s+(?:my\s+)?(?:income\s+)?tax\b|"
    r"\bincome\s+tax\b|"
    r"\btax\s+returns?\b|"
    r"\bitr\b|"
    r"\b(?:my|your)\s+holdings?\b|"
    r"\bgroww\s+login\b|"
    r"\blogin\b|\blog\s+in\b|"
    r"\bexecute\s+(?:a\s+)?(?:sip|order|transaction)\b|"
    r"\bplace\s+(?:an?\s+)?order\b|"
    r"\bmy\s+orders?\b",
    re.IGNORECASE,
)
_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("expense_ratio", re.compile(r"\b(?:expense\s+ratio|total\s+expense|\bter\b)\b", re.IGNORECASE)),
    ("exit_load", re.compile(r"\bexit\s+load\b", re.IGNORECASE)),
    ("minimum_sip", re.compile(r"\b(?:minimum\s+sip|min\.?\s+sip|sip\s+minimum)\b", re.IGNORECASE)),
    (
        "minimum_lumpsum",
        re.compile(
            r"\b(?:minimum\s+lump\s*sum|lump\s*sum|minimum\s+investment|min\.?\s+investment)\b",
            re.IGNORECASE,
        ),
    ),
    ("benchmark", re.compile(r"\bbenchmark\b", re.IGNORECASE)),
    ("riskometer", re.compile(r"\briskometer\b", re.IGNORECASE)),
    (
        "lock_in",
        re.compile(
            r"\block[\s-]*in\b|\bcan\s+i\s+break\b|\bbreak\b.{0,24}\b(?:elss|tax)\b",
            re.IGNORECASE,
        ),
    ),
)
_DOWNLOAD = re.compile(
    r"\b(?:download|capital[\s-]*gains?(?:\s+statement)?|account\s+statement|"
    r"consolidated\s+account\s+statement|\bcas\b)\b",
    re.IGNORECASE,
)
_INDEFINITE = re.compile(r"\bwhat\s+is\s+an?\b|\bwhat\s+are\b|\bdefine\b|\bdefinition\b|\bmeaning\s+of\b", re.IGNORECASE)
_DEFINITE = re.compile(r"\bwhat\s+is\s+the\b", re.IGNORECASE)
# Dropped from the UTI-prefixed form when the remainder is too generic to be a scheme.
_GENERIC_ALIASES = frozenset(
    {
        "short term",
        "long term",
        "equity fund",
        "balanced fund",
        "index fund",
        "hybrid fund",
    }
)
_NEGATED_PREFIX = re.compile(
    r"(?:^|[.?!;,]|\b(?:and|or|but)\b)\s*"
    r"(?:not|no|neither|nor|except|without|instead\s+of|rather\s+than|other\s+than)"
    r"\s+(?:the\s+|a\s+|an\s+)?$",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


@dataclass(frozen=True)
class GuardResult:
    """One class for this message, plus the scheme this call resolved.

    `scheme_id` is that one scheme. It is None when several schemes are named,
    or when this call has no scheme. `field` is a fact name, `download`, or
    `definition`. It is never a figure.
    """

    intent: str
    scheme_id: str | None
    field: str | None


def classify(text: str, corpus: Corpus, active_scheme_id: str | None = None) -> GuardResult:
    """Classify `text` using `active_scheme_id` only as a fallback for this call."""
    message = text or ""
    positive = _positive_scheme_ids(message, corpus)
    active = _known(corpus, active_scheme_id)
    if len(positive) == 1:
        scheme_id: str | None = positive[0]
    elif len(positive) == 0:
        scheme_id = active
    else:
        scheme_id = None
    field = _detect_field(message, scheme_named=bool(positive))

    if contains_pii(message):
        return GuardResult("pii", scheme_id, field)
    if _ADVICE.search(message):
        return GuardResult("advice", scheme_id, field)
    if len(positive) >= 2 or _ALL_SCHEMES.search(message) or _COMPARISON.search(message):
        single = scheme_id if len(positive) == 1 and not _ALL_SCHEMES.search(message) else None
        return GuardResult("comparison", single, field)
    if _performance(message):
        return GuardResult("performance", scheme_id, field)
    if _out_of_scope(message):
        # "HDFC Flexi Cap" contains an in-scope alias. Do not bind that UTI scheme.
        if _AMC.search(message) or _foreign_url(message):
            scheme_id = None
        return GuardResult("out_of_scope", scheme_id, field)
    if field in FACT_FIELDS:
        if scheme_id is None:
            return GuardResult("clarify_scheme", None, field)
        return GuardResult("factual", scheme_id, field)
    if field in {"download", "definition"}:
        return GuardResult("factual", scheme_id, field)
    named = scheme_id if len(positive) == 1 else None
    return GuardResult("clarify_scheme", named, None)


def _known(corpus: Corpus, scheme_id: str | None) -> str | None:
    if scheme_id is None:
        return None
    try:
        corpus.scheme(scheme_id)
    except KeyError:
        return None
    return scheme_id


def _performance(text: str) -> bool:
    without_tax_return = _TAX_RETURN.sub(" ", text)
    return _PERFORMANCE.search(without_tax_return) is not None


def _out_of_scope(text: str) -> bool:
    if _AMC.search(text) or _SCOPE.search(text):
        return True
    return _foreign_url(text)


def _detect_field(text: str, scheme_named: bool) -> str | None:
    found = None
    for name, pattern in _FIELD_PATTERNS:
        if pattern.search(text):
            found = name
            break
    if found is not None and _pure_definition(text, scheme_named):
        return "definition"
    if found is not None:
        return found
    if _DOWNLOAD.search(text):
        return "download"
    if _INDEFINITE.search(text):
        return "definition"
    return None


def _pure_definition(text: str, scheme_named: bool) -> bool:
    if scheme_named or _DEFINITE.search(text):
        return False
    return _INDEFINITE.search(text) is not None


def _positive_scheme_ids(text: str, corpus: Corpus) -> tuple[str, ...]:
    haystack = normalize_alias(text)
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    for alias, scheme_ids in _alias_table(corpus):
        pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
        for match in re.finditer(pattern, haystack):
            start, end = match.span()
            if any(kept_start <= start and end <= kept_end for kept_start, kept_end, _ids in spans):
                continue
            spans = [item for item in spans if not (start <= item[0] and item[1] <= end)]
            spans.append((start, end, scheme_ids))
    ordered: list[str] = []
    for start, _end, scheme_ids in sorted(spans):
        if _NEGATED_PREFIX.search(haystack[:start]):
            continue
        for scheme_id in scheme_ids:
            if scheme_id not in ordered:
                ordered.append(scheme_id)
    for scheme_id in _url_scheme_ids(text, corpus):
        if scheme_id not in ordered:
            ordered.append(scheme_id)
    return tuple(ordered)


def _alias_table(corpus: Corpus) -> list[tuple[str, tuple[str, ...]]]:
    """Registry aliases plus a leading 'UTI' dropped, when that phrase is unique."""
    table: dict[str, set[str]] = {}
    for alias, scheme_id in corpus.alias_index.items():
        table.setdefault(alias, set()).add(scheme_id)
    for alias, scheme_ids in corpus.ambiguous_aliases.items():
        table.setdefault(alias, set()).update(scheme_ids)
    derived: dict[str, set[str]] = {}
    for scheme in corpus.schemes:
        for label in (scheme.name, *scheme.aliases):
            key = normalize_alias(label)
            if not key.startswith("uti "):
                continue
            rest = re.sub(r"\s+direct growth$", "", key[4:]).strip()
            rest = re.sub(r"\s*\([^)]*\)", " ", rest)
            rest = re.sub(r"\s+", " ", rest).strip()
            candidates = [rest]
            if rest.endswith(" fund"):
                shorter = rest[: -len(" fund")].strip()
                if len(shorter.split()) >= 2:
                    candidates.append(shorter)
            for candidate in candidates:
                if len(candidate.split()) < 2 or candidate in _GENERIC_ALIASES:
                    continue
                derived.setdefault(candidate, set()).add(scheme.scheme_id)
    for alias, scheme_ids in derived.items():
        if len(scheme_ids) == 1 and alias not in table:
            table[alias] = set(scheme_ids)
    ranked = sorted(table.items(), key=lambda item: len(item[0]), reverse=True)
    return [(alias, tuple(sorted(ids))) for alias, ids in ranked]


def _url_scheme_ids(text: str, corpus: Corpus) -> list[str]:
    found: list[str] = []
    for match in _URL.finditer(text):
        raw = match.group(0).rstrip(".,);]")
        if _NEGATED_PREFIX.search(normalize_alias(text[: match.start()])):
            continue
        for scheme_id in _schemes_for_url(raw, corpus):
            if scheme_id not in found:
                found.append(scheme_id)
    return found


def _schemes_for_url(url: str, corpus: Corpus) -> tuple[str, ...]:
    cleaned = url.strip()
    ids: list[str] = []
    for scheme in corpus.schemes:
        if cleaned in {scheme.factsheet_url, scheme.primary_url, scheme.groww_url}:
            ids.append(scheme.scheme_id)
    for document in corpus.documents:
        if document.url == cleaned and document.scheme_id and document.scheme_id not in ids:
            ids.append(document.scheme_id)
    return tuple(ids)


def _foreign_url(text: str) -> bool:
    for match in _URL.finditer(text):
        raw = match.group(0).rstrip(".,);]")
        host = (urlsplit(raw).hostname or "").lower().rstrip(".")
        if not host or host in ALLOWLIST_HOSTS or host in GROWW_HOSTS:
            continue
        return True
    return False

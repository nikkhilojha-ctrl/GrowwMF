"""Scheme registry, corpus manifest, and host allowlist."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from app.settings import ROOT

ALLOWLIST_HOSTS = frozenset(
    {
        "utimf.com",
        "www.utimf.com",
        "amfiindia.com",
        "www.amfiindia.com",
        "sebi.gov.in",
        "www.sebi.gov.in",
        "investor.sebi.gov.in",
    }
)
DOC_TYPES = frozenset(
    {
        "factsheet",
        "kim",
        "sid",
        "amc_faq",
        "statement_guide",
        "tax_guide",
        "amfi",
        "sebi",
    }
)
SCHEME_FIELDS = (
    "scheme_id",
    "name",
    "aliases",
    "category",
    "groww_url",
    "factsheet_url",
    "primary_url",
)
GROWW_HOSTS = frozenset({"groww.in", "www.groww.in"})


class CorpusLoadError(ValueError):
    """A registry, manifest, or education file broke the corpus rules."""


@dataclass(frozen=True)
class Scheme:
    scheme_id: str
    name: str
    aliases: tuple[str, ...]
    category: str
    groww_url: str
    factsheet_url: str
    primary_url: str


@dataclass(frozen=True)
class Document:
    url: str
    scheme_id: str | None
    doc_type: str
    title: str


@dataclass(frozen=True)
class Corpus:
    schemes: tuple[Scheme, ...]
    documents: tuple[Document, ...]
    amfi_education_url: str
    sebi_education_url: str
    alias_index: dict[str, str]
    ambiguous_aliases: dict[str, tuple[str, ...]]

    @property
    def scheme_count(self) -> int:
        return len(self.schemes)

    @property
    def document_count(self) -> int:
        return len(self.documents)

    def scheme(self, scheme_id: str) -> Scheme:
        for item in self.schemes:
            if item.scheme_id == scheme_id:
                return item
        raise KeyError(scheme_id)

    def factsheet_index_url(self) -> str:
        for document in self.documents:
            if document.scheme_id is None and document.doc_type == "factsheet":
                return document.url
        raise CorpusLoadError("manifest has no shared factsheet index")

    def schemes_named_in(self, text: str) -> tuple[str, ...]:
        """Whole-alias matches. A longer alias hides a shorter one inside it."""
        haystack = normalize_alias(text)
        ranked = sorted(
            [(alias, (scheme_id,)) for alias, scheme_id in self.alias_index.items()]
            + [(alias, scheme_ids) for alias, scheme_ids in self.ambiguous_aliases.items()],
            key=lambda item: len(item[0]),
            reverse=True,
        )
        spans: list[tuple[int, int, tuple[str, ...]]] = []
        for alias, scheme_ids in ranked:
            pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
            for match in re.finditer(pattern, haystack):
                start, end = match.span()
                if any(kept_start <= start and end <= kept_end for kept_start, kept_end, _ in spans):
                    continue
                spans = [item for item in spans if not (start <= item[0] and item[1] <= end)]
                spans.append((start, end, scheme_ids))
        ordered: list[str] = []
        for _, _, scheme_ids in sorted(spans):
            for scheme_id in scheme_ids:
                if scheme_id not in ordered:
                    ordered.append(scheme_id)
        return tuple(ordered)


def normalize_alias(text: str) -> str:
    collapsed = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", collapsed).strip()


def check_official_url(url: str) -> str:
    """Accept only http(s) URLs whose hostname is one of the seven allowlist names."""
    if not isinstance(url, str) or not url.strip():
        raise CorpusLoadError("missing URL")
    cleaned = url.strip()
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise CorpusLoadError(f"URL scheme is not allowed: {cleaned}")
    if parsed.username is not None or parsed.password is not None:
        raise CorpusLoadError(f"URL user info is not allowed: {cleaned}")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname or _is_ip(hostname) or hostname not in ALLOWLIST_HOSTS:
        raise CorpusLoadError(f"URL host is not on the allowlist: {cleaned}")
    if parsed.port not in {None, 443}:
        raise CorpusLoadError(f"URL port is not allowed: {cleaned}")
    return cleaned


def load_corpus(directory: Path | None = None) -> Corpus:
    folder = directory or (ROOT / "corpus")
    schemes = _load_schemes(folder / "schemes.yaml")
    documents = _load_manifest(folder / "manifest.yaml", {item.scheme_id for item in schemes})
    amfi_url, sebi_url = _load_education(folder / "education.yaml", {item.url for item in documents})
    alias_index, ambiguous = _alias_index(schemes)
    return Corpus(
        schemes=schemes,
        documents=documents,
        amfi_education_url=amfi_url,
        sebi_education_url=sebi_url,
        alias_index=alias_index,
        ambiguous_aliases=ambiguous,
    )


def _load_schemes(path: Path) -> tuple[Scheme, ...]:
    rows = _read_yaml_list(path, "schemes.yaml")
    schemes: list[Scheme] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CorpusLoadError("each scheme must be a mapping")
        missing = [field for field in SCHEME_FIELDS if field not in row]
        if missing:
            raise CorpusLoadError(f"scheme is missing {', '.join(missing)}")
        scheme_id = _text(row["scheme_id"], "scheme_id")
        if scheme_id in seen:
            raise CorpusLoadError(f"duplicate scheme_id: {scheme_id}")
        seen.add(scheme_id)
        aliases = row["aliases"]
        if not isinstance(aliases, list) or not aliases:
            raise CorpusLoadError(f"{scheme_id} needs at least one alias")
        scheme = Scheme(
            scheme_id=scheme_id,
            name=_text(row["name"], "name"),
            aliases=tuple(_text(alias, "alias") for alias in aliases),
            category=_text(row["category"], "category"),
            groww_url=_check_groww_url(row["groww_url"]),
            factsheet_url=check_official_url(row["factsheet_url"]),
            primary_url=check_official_url(row["primary_url"]),
        )
        schemes.append(scheme)
    if not schemes:
        raise CorpusLoadError("schemes.yaml has no schemes")
    return tuple(schemes)


def _load_manifest(path: Path, scheme_ids: set[str]) -> tuple[Document, ...]:
    rows = _read_yaml_list(path, "manifest.yaml")
    documents: list[Document] = []
    seen_urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CorpusLoadError("each manifest entry must be a mapping")
        url = check_official_url(_text(row.get("url"), "url"))
        if url in seen_urls:
            raise CorpusLoadError(f"duplicate manifest URL: {url}")
        seen_urls.add(url)
        raw_scheme = row.get("scheme_id")
        scheme_id = None if raw_scheme in {None, ""} else _text(raw_scheme, "scheme_id")
        if scheme_id is not None and scheme_id not in scheme_ids:
            raise CorpusLoadError(f"manifest scheme_id is not in the registry: {scheme_id}")
        doc_type = _text(row.get("doc_type"), "doc_type")
        if doc_type not in DOC_TYPES:
            raise CorpusLoadError(f"unknown doc_type: {doc_type}")
        documents.append(
            Document(
                url=url,
                scheme_id=scheme_id,
                doc_type=doc_type,
                title=_text(row.get("title"), "title"),
            )
        )
    if not documents:
        raise CorpusLoadError("manifest.yaml has no documents")
    return tuple(documents)


def _load_education(path: Path, manifest_urls: set[str]) -> tuple[str, str]:
    payload = _read_yaml_mapping(path, "education.yaml")
    expected = {"amfi_url", "sebi_url"}
    if set(payload) != expected:
        raise CorpusLoadError("education.yaml must contain exactly amfi_url and sebi_url")
    amfi_url = check_official_url(_text(payload["amfi_url"], "amfi_url"))
    sebi_url = check_official_url(_text(payload["sebi_url"], "sebi_url"))
    if "amfiindia.com" not in urlsplit(amfi_url).hostname:
        raise CorpusLoadError("amfi_url must be on amfiindia.com")
    host = (urlsplit(sebi_url).hostname or "")
    if host not in {"sebi.gov.in", "www.sebi.gov.in", "investor.sebi.gov.in"}:
        raise CorpusLoadError("sebi_url must be on a SEBI host")
    for url in (amfi_url, sebi_url):
        if url not in manifest_urls:
            raise CorpusLoadError(f"education URL is missing from the manifest: {url}")
    return amfi_url, sebi_url


def _alias_index(schemes: tuple[Scheme, ...]) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    buckets: dict[str, set[str]] = {}
    for scheme in schemes:
        for label in (scheme.name, *scheme.aliases):
            key = normalize_alias(label)
            if not key:
                continue
            buckets.setdefault(key, set()).add(scheme.scheme_id)
    unique = {key: next(iter(ids)) for key, ids in buckets.items() if len(ids) == 1}
    ambiguous = {key: tuple(sorted(ids)) for key, ids in buckets.items() if len(ids) > 1}
    return unique, ambiguous


def _check_groww_url(url: object) -> str:
    cleaned = _text(url, "groww_url")
    parsed = urlsplit(cleaned)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in GROWW_HOSTS:
        raise CorpusLoadError(f"groww_url must be an https groww.in reference: {cleaned}")
    return cleaned


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusLoadError(f"{field} must be a non-empty string")
    return value.strip()


def _read_yaml_list(path: Path, label: str) -> list:
    payload = _read_yaml(path, label)
    if not isinstance(payload, list):
        raise CorpusLoadError(f"{label} must be a list")
    return payload


def _read_yaml_mapping(path: Path, label: str) -> dict:
    payload = _read_yaml(path, label)
    if not isinstance(payload, dict):
        raise CorpusLoadError(f"{label} must be a mapping")
    return payload


def _read_yaml(path: Path, label: str):
    if not path.is_file():
        raise CorpusLoadError(f"missing {label}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if payload is None:
        raise CorpusLoadError(f"{label} is empty")
    return payload


def _is_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True

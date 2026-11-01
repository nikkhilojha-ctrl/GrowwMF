"""Allowlisted GET. Redirects that leave the host list fail the document."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.schemes import CorpusLoadError, check_official_url

MAX_BYTES = 15 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    """The document could not be downloaded inside the allowlist."""


@dataclass(frozen=True)
class Fetched:
    url: str
    body: bytes
    content_type: str


def fetch_url(
    url: str,
    client: httpx.Client,
    max_bytes: int = MAX_BYTES,
) -> Fetched:
    """GET `url`, following redirects only while every hop stays on the allowlist."""
    current = _allowlisted(url, redirect=False)
    for _hop in range(MAX_REDIRECTS + 1):
        with client.stream(
            "GET",
            current,
            follow_redirects=False,
            timeout=TIMEOUT_SECONDS,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/pdf,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        ) as response:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(f"redirect without a location: {current}")
                current = _allowlisted(urljoin(current, location.strip()), redirect=True)
                continue
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code} for {current}")
            _reject_declared_length(response.headers.get("content-length"), max_bytes)
            body = _read_capped(response, max_bytes)
            content_type = response.headers.get("content-type", "")
            return Fetched(url=current, body=body, content_type=content_type)
    raise FetchError(f"too many redirects for {url}")


def _allowlisted(url: str, *, redirect: bool) -> str:
    try:
        return check_official_url(url)
    except CorpusLoadError as exc:
        label = "redirect host is not on the allowlist" if redirect else "host is not on the allowlist"
        raise FetchError(f"{label}: {url}") from exc


def _reject_declared_length(header: str | None, max_bytes: int) -> None:
    if header is None:
        return
    try:
        declared = int(header)
    except ValueError:
        return
    if declared > max_bytes:
        raise FetchError("response exceeds the size cap")


def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for piece in response.iter_bytes():
        total += len(piece)
        if total > max_bytes:
            raise FetchError("response exceeds the size cap")
        chunks.append(piece)
    return b"".join(chunks)

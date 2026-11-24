"""Thread API. Every message read and write is limited to one thread_id.

Threads belong to one anonymous browser, identified by an HttpOnly cookie, so a
visitor only ever sees the threads that browser created.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from app.db import display_date, read_db, transaction, utc_now
from app.ratelimit import enforce_message_limit
from app.guard.intent import GuardResult, classify
from app.rag.generate import Answer, answer_from_retrieval
from app.rag.retrieve import retrieve
from app.rag.templates import reply_for, source_for
from app.schemes import Corpus

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

NEW_CONVERSATION_TITLE = "New conversation"
REDACTED_USER_TEXT = "[removed: personal data]"
MAX_MESSAGE_CHARS = 1000
TITLE_MAX = 80
CLIENT_COOKIE = "mf_client"
CLIENT_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

INSERT_THREAD = """
INSERT INTO threads (thread_id, title, active_scheme_id, created_at, updated_at, client_id)
VALUES (?, ?, NULL, ?, ?, ?)
"""

LIST_THREADS = """
SELECT thread_id, title, active_scheme_id, updated_at
FROM threads
WHERE client_id = ?
ORDER BY updated_at DESC, rowid DESC
"""

SELECT_THREAD = """
SELECT thread_id, title, active_scheme_id, created_at, updated_at
FROM threads
WHERE thread_id = ?
"""

SELECT_OWN_THREAD = """
SELECT thread_id, title, active_scheme_id, created_at, updated_at
FROM threads
WHERE thread_id = ? AND client_id = ?
"""

UPDATE_THREAD = """
UPDATE threads
SET title = ?, active_scheme_id = ?, updated_at = ?
WHERE thread_id = ?
"""

INSERT_USER_MESSAGE = """
INSERT INTO messages (
    message_id, thread_id, role, kind, text, citation_url, footer, created_at
) VALUES (?, ?, 'user', NULL, ?, NULL, NULL, ?)
"""

INSERT_ASSISTANT_MESSAGE = """
INSERT INTO messages (
    message_id, thread_id, role, kind, text, citation_url, footer, created_at
) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?)
"""

SELECT_MESSAGES = """
SELECT role, kind, text, citation_url, footer, created_at
FROM messages
WHERE thread_id = ?
ORDER BY rowid ASC
"""


class ThreadCreated(BaseModel):
    thread_id: str
    title: str
    created_at: str


class ThreadSummary(BaseModel):
    thread_id: str
    title: str
    active_scheme_id: str | None
    updated_at: str


class Citation(BaseModel):
    url: str
    title: str | None = None
    doc_type: str | None = None


class MessageOut(BaseModel):
    role: str
    kind: str | None
    text: str
    citation: Citation | None
    footer: str | None
    created_at: str


class ThreadDetail(BaseModel):
    thread_id: str
    title: str
    active_scheme_id: str | None
    created_at: str
    updated_at: str
    messages: list[MessageOut]


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str


class PostedMessage(BaseModel):
    thread_id: str
    message_id: str
    kind: str
    scheme_id: str | None
    text: str
    citation: Citation | None
    footer: str | None


def database_path(request: Request) -> Path:
    return request.app.state.database_path


def client_id(request: Request, response: Response) -> str:
    """The browser's anonymous id. A missing or malformed cookie gets a new one."""
    raw = request.cookies.get(CLIENT_COOKIE, "")
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        pass
    new_id = str(uuid.uuid4())
    response.set_cookie(
        CLIENT_COOKIE,
        new_id,
        max_age=CLIENT_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return new_id


@router.post("/threads", response_model=ThreadCreated, response_model_exclude_none=False)
def create_thread(
    path: Path = Depends(database_path),
    client: str = Depends(client_id),
) -> ThreadCreated:
    thread_id = str(uuid.uuid4())
    created_at = utc_now()
    with transaction(path) as conn:
        conn.execute(
            INSERT_THREAD,
            (thread_id, NEW_CONVERSATION_TITLE, created_at, created_at, client),
        )
    return ThreadCreated(thread_id=thread_id, title=NEW_CONVERSATION_TITLE, created_at=created_at)


@router.get("/threads", response_model=list[ThreadSummary], response_model_exclude_none=False)
def list_threads(
    path: Path = Depends(database_path),
    client: str = Depends(client_id),
) -> list[ThreadSummary]:
    with read_db(path) as conn:
        rows = conn.execute(LIST_THREADS, (client,)).fetchall()
    return [
        ThreadSummary(
            thread_id=row["thread_id"],
            title=row["title"],
            active_scheme_id=row["active_scheme_id"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


@router.get(
    "/threads/{thread_id}",
    response_model=ThreadDetail,
    response_model_exclude_none=False,
)
def get_thread(
    thread_id: str,
    request: Request,
    path: Path = Depends(database_path),
    client: str = Depends(client_id),
) -> ThreadDetail:
    with read_db(path) as conn:
        row = _thread_row(conn, thread_id, client)
        messages = _messages_for_thread(conn, thread_id)
    return _thread_detail(row, messages, request.app.state.corpus)


@router.post(
    "/threads/{thread_id}/messages",
    response_model=PostedMessage,
    response_model_exclude_none=False,
)
def post_message(
    thread_id: str,
    body: MessageRequest,
    request: Request,
    path: Path = Depends(database_path),
    client: str = Depends(client_id),
) -> PostedMessage:
    text = _validated_text(body.text)
    corpus: Corpus = request.app.state.corpus
    settings = request.app.state.settings
    with read_db(path) as conn:
        thread = _thread_row(conn, thread_id, client)
    enforce_message_limit(request)
    result = classify(text, corpus, thread["active_scheme_id"])
    now = utc_now()
    if result.intent == "pii":
        log.info("personal-data refusal")
        answer = _template_answer(result, corpus, path)
        user_text = REDACTED_USER_TEXT
        title = None
    elif result.intent != "factual":
        answer = _template_answer(result, corpus, path)
        user_text = text
        title = None
    else:
        answer = _factual_answer(text, result, corpus, settings, path)
        user_text = text
        title = _conversation_title(text)
    assistant_id = _persist(
        path,
        thread_id=thread_id,
        user_text=user_text,
        answer=answer,
        scheme_id=result.scheme_id,
        title=title,
        now=now,
    )
    return PostedMessage(
        thread_id=thread_id,
        message_id=assistant_id,
        kind=answer.kind,
        scheme_id=answer.scheme_id,
        text=answer.text,
        citation=_citation_out(corpus, answer.citation_url),
        footer=answer.footer,
    )


def _validated_text(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is empty")
    if len(raw) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="text exceeds 1000 characters")
    return text


def _thread_row(
    conn: sqlite3.Connection,
    thread_id: str,
    client: str | None = None,
) -> sqlite3.Row:
    """Another browser's thread is reported as missing, never as forbidden."""
    if client is None:
        row = conn.execute(SELECT_THREAD, (thread_id,)).fetchone()
    else:
        row = conn.execute(SELECT_OWN_THREAD, (thread_id, client)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return row


def _messages_for_thread(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(SELECT_MESSAGES, (thread_id,)))


def _template_answer(result: GuardResult, corpus: Corpus, path: Path) -> Answer:
    reply = reply_for(result, corpus, _date_for(path, _template_url(result, corpus)))
    if reply is None:
        raise HTTPException(status_code=500, detail="no template")
    return Answer(
        kind=reply.kind,
        text=reply.text,
        citation_url=reply.citation_url,
        citation_title=reply.citation_title,
        doc_type=reply.doc_type,
        footer=reply.footer,
        scheme_id=result.scheme_id,
    )


def _factual_answer(text, result: GuardResult, corpus: Corpus, settings, path: Path) -> Answer:
    scheme = None if result.scheme_id is None else corpus.scheme(result.scheme_id)
    retrieval = retrieve(text, result.scheme_id, result.field, path)
    missing_url = _missing_url(corpus, result.scheme_id, result.field)
    return answer_from_retrieval(
        text,
        scheme,
        result.field,
        retrieval,
        corpus,
        settings,
        _date_for(path, missing_url),
        pii_date=_date_for(path, corpus.sebi_education_url),
    )


def _template_url(result: GuardResult, corpus: Corpus) -> str:
    if result.intent == "pii":
        return corpus.sebi_education_url
    if result.intent == "performance":
        if result.scheme_id:
            return corpus.scheme(result.scheme_id).factsheet_url
        return corpus.factsheet_index_url()
    if result.intent == "clarify_scheme":
        return corpus.factsheet_index_url()
    return corpus.amfi_education_url


def _missing_url(corpus: Corpus, scheme_id: str | None, field: str | None) -> str:
    if scheme_id:
        return corpus.scheme(scheme_id).primary_url
    if field == "download":
        for document in corpus.documents:
            if document.doc_type in {"statement_guide", "tax_guide"}:
                return document.url
    return corpus.factsheet_index_url()


def _date_for(path: Path, url: str) -> str:
    with read_db(path) as conn:
        stamp = _source_timestamp(conn, url)
    return display_date(stamp or utc_now())


def _source_timestamp(conn: sqlite3.Connection, url: str) -> str | None:
    for statement in (
        "SELECT retrieved_at FROM chunks WHERE url = ? ORDER BY retrieved_at DESC LIMIT 1",
        "SELECT retrieved_at FROM scheme_facts WHERE source_url = ? ORDER BY retrieved_at DESC LIMIT 1",
    ):
        row = conn.execute(statement, (url,)).fetchone()
        if row is not None and row[0]:
            return row[0]
    row = conn.execute("SELECT last_ingest FROM ingest_state WHERE id = 1").fetchone()
    if row is not None and row[0]:
        return row[0]
    return None


def _conversation_title(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= TITLE_MAX:
        return collapsed
    return collapsed[:TITLE_MAX].rstrip()


def _persist(
    path: Path,
    *,
    thread_id: str,
    user_text: str,
    answer: Answer,
    scheme_id: str | None,
    title: str | None,
    now: str,
) -> str:
    with transaction(path) as conn:
        return _write_turn(
            conn,
            thread_id=thread_id,
            user_text=user_text,
            answer=answer,
            scheme_id=scheme_id,
            title=title,
            now=now,
        )


def _write_turn(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    user_text: str,
    answer: Answer,
    scheme_id: str | None,
    title: str | None,
    now: str,
) -> str:
    row = _thread_row(conn, thread_id)
    stored_scheme = scheme_id or row["active_scheme_id"]
    if title and row["title"] == NEW_CONVERSATION_TITLE:
        stored_title = title
    else:
        stored_title = row["title"]
    assistant_id = str(uuid.uuid4())
    conn.execute(
        INSERT_USER_MESSAGE,
        (str(uuid.uuid4()), thread_id, user_text, now),
    )
    conn.execute(
        INSERT_ASSISTANT_MESSAGE,
        (
            assistant_id,
            thread_id,
            answer.kind,
            answer.text,
            answer.citation_url,
            answer.footer,
            now,
        ),
    )
    conn.execute(UPDATE_THREAD, (stored_title, stored_scheme, now, thread_id))
    return assistant_id


def _thread_detail(row: sqlite3.Row, messages: list[sqlite3.Row], corpus: Corpus) -> ThreadDetail:
    return ThreadDetail(
        thread_id=row["thread_id"],
        title=row["title"],
        active_scheme_id=row["active_scheme_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        messages=[_message_out(message, corpus) for message in messages],
    )


def _message_out(row: sqlite3.Row, corpus: Corpus) -> MessageOut:
    citation = _citation_out(corpus, row["citation_url"]) if row["citation_url"] else None
    return MessageOut(
        role=row["role"],
        kind=row["kind"],
        text=row["text"],
        citation=citation,
        footer=row["footer"],
        created_at=row["created_at"],
    )


def _citation_out(corpus: Corpus, url: str) -> Citation:
    title, doc_type = source_for(corpus, url)
    return Citation(url=url, title=title, doc_type=doc_type)

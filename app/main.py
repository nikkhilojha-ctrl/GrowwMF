"""FastAPI app: health check, thread API, and the static page."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.db import corpus_status, init_database, read_db
from app.schemes import load_corpus
from app.settings import get_settings
from app.threads import router as threads_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class HealthResponse(BaseModel):
    ok: bool
    schemes: int
    documents: int
    last_ingest: str | None


class SchemeSummary(BaseModel):
    scheme_id: str
    name: str
    category: str
    groww_url: str


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_database(application.state.database_path)
    yield


def list_schemes(request: Request) -> list[SchemeSummary]:
    """Groww URLs are reference links for the sidebar, never citations."""
    return [
        SchemeSummary(
            scheme_id=scheme.scheme_id,
            name=scheme.name,
            category=scheme.category,
            groww_url=scheme.groww_url,
        )
        for scheme in request.app.state.corpus.schemes
    ]


def health(request: Request) -> HealthResponse:
    with read_db(request.app.state.database_path) as conn:
        schemes, documents, last_ingest = corpus_status(conn)
    return HealthResponse(
        ok=True,
        schemes=schemes,
        documents=documents,
        last_ingest=last_ingest,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title="Mutual Fund FAQ Assistant", lifespan=lifespan)
    application.state.settings = settings
    application.state.database_path = settings.database_path
    application.state.corpus = load_corpus()
    application.include_router(threads_router)
    application.add_api_route(
        "/api/schemes",
        list_schemes,
        methods=["GET"],
        response_model=list[SchemeSummary],
        response_model_exclude_none=False,
    )
    application.add_api_route(
        "/api/health",
        health,
        methods=["GET"],
        response_model=HealthResponse,
        response_model_exclude_none=False,
    )
    # Mount after API routes so "/" does not swallow /api.
    application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return application


app = create_app()

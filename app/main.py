"""FastAPI entrypoint: app factory, router registration, startup/shutdown wiring."""
# Implemented in M1; M2 added the approvals router. M3 adds the semantic-cache pre-check; M6 Langfuse.

from __future__ import annotations

import logging

from dotenv import load_dotenv

# Load .env before any module reads os.environ (app.db.database, LiteLLM's OPENAI_API_KEY, …).
load_dotenv()

from fastapi import FastAPI  # noqa: E402

from app.api import routes_approvals, routes_chat, routes_health  # noqa: E402
from app.config import get_settings  # noqa: E402


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="agentdesk",
        description="AI-powered ITSM service desk — router + specialist agents over hybrid RAG.",
        version="0.1.0",
    )
    app.include_router(routes_health.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_approvals.router)
    return app


app = create_app()

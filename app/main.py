"""FastAPI entrypoint: app factory, router registration, startup/shutdown wiring."""
# Implemented in M1; M2 added the approvals router. M3 added the semantic-cache pre-check;
# M6 the Langfuse tracing bridge (init at app construction, exporter drained on shutdown).

from __future__ import annotations

import contextlib
import logging

from dotenv import load_dotenv

# Load .env before any module reads os.environ (app.db.database, LiteLLM's OPENAI_API_KEY, …).
load_dotenv()

from fastapi import FastAPI  # noqa: E402

from app.api import routes_approvals, routes_chat, routes_health  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.observability import tracing  # noqa: E402


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    # Langfuse exports from a background batch thread; drain it so a stopping worker (deploys,
    # the suite-spawned e2e/slack servers) doesn't lose its last spans. No-op when keys are
    # empty — flush() only touches a client that was actually created.
    tracing.flush()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    # M6 (ADR-042): registers the Langfuse trace processor — or logs one line and does NOTHING
    # when keys are empty (CI, keyless local dev). Must precede any Runner.run in this process.
    tracing.init_tracing()

    app = FastAPI(
        title="agentdesk",
        description="AI-powered ITSM service desk — router + specialist agents over hybrid RAG.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.include_router(routes_health.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_approvals.router)
    return app


app = create_app()

"""Short-term memory: OpenAI Agents SDK sessions backed by Postgres."""
# Implemented in M5 (ADR-030), replacing the ADR-019 sqlite stopgap: the pinned SDK (0.17.7)
# ships SQLAlchemySession, so this module only owns the engine and the constructor defaults —
# no hand-rolled Session protocol. The tables (agent_sessions/agent_messages) are created by
# Alembic migration 0002, which mirrors the SDK's schema exactly; create_tables stays False so
# Alembic remains the single owner of the database schema.

from __future__ import annotations

from agents.extensions.memory import SQLAlchemySession
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.database import DATABASE_URL

# One async engine per process (mirrors app/db/database.py's sync engine). The same
# postgresql+psycopg:// URL serves both: SQLAlchemy's psycopg dialect is sync AND async, so no
# second driver (asyncpg) enters the dependency tree.
_engine: AsyncEngine | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


def get_session_store(session_id: str) -> SQLAlchemySession:
    """Session handle for one conversation. Same id = same history, from any process — the
    property the sqlite stopgap (git-ignored local file) could never give a deploy."""
    return SQLAlchemySession(session_id, engine=_get_engine(), create_tables=False)

"""SQLAlchemy engine, session factory, and the declarative Base shared by all models and tools."""
# Implemented in M0. Tools (M2) acquire sessions from here — this is the single connection point
# behind the "tools are the only DB access path" invariant (ADR-004).

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Kept independent of app.config (an M1 concern) so M0 scripts and migrations can run standalone.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://itsm:itsm@localhost:5432/itsm"
)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for every ORM model; `Base.metadata` is what Alembic reflects against."""


def get_session() -> Iterator[Session]:
    """Yield a session and guarantee it is closed (FastAPI dependency / script context)."""
    with SessionLocal() as session:
        yield session

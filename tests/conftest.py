"""Shared pytest fixtures: test DB session, fake Redis, sample rows."""
# M1 fixtures: live-DB session (skipping when the M0 docker DB is down) + in-memory fake Redis.
# TODO(M2+): sample-row factories as tool tests land.

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.database import SessionLocal, engine


def db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not db_available(), reason="Postgres not reachable (run `make db-up && make seed`)"
)


@pytest.fixture
def db_session():
    with SessionLocal() as session:
        yield session


class FakeRedis:
    """Minimal stand-in implementing the subset the embedding cache uses (mget/set/pipeline)."""

    def __init__(self) -> None:
        self.store: dict[bytes, bytes] = {}

    def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def set(self, key, value):
        self.store[key] = value

    def pipeline(self, transaction=True):  # noqa: ARG002 — signature parity with redis-py
        return _FakePipeline(self)

    def ping(self):
        return True


class _FakePipeline:
    def __init__(self, parent: FakeRedis) -> None:
        self.parent = parent
        self.ops: list[tuple[bytes, bytes]] = []

    def set(self, key, value):
        self.ops.append((key, value))
        return self

    def execute(self):
        for k, v in self.ops:
            self.parent.set(k, v)
        return [True] * len(self.ops)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()

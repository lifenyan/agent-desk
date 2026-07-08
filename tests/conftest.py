"""Shared pytest fixtures: test DB session, fake Redis, sample rows."""
# M1 fixtures: live-DB session (skipping when the M0 docker DB is down) + in-memory fake Redis.
# M3 extended FakeRedis (get/setex/delete/incrby/ttl/scan_iter + a generic pipeline) and added
# DownRedis for degradation tests.

from __future__ import annotations

import os
from fnmatch import fnmatch

import pytest
import redis
from sqlalchemy import text

from app.db.database import SessionLocal, engine


def db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


_DB_AVAILABLE = db_available()

# CI guard (M4): locally, DB-backed tests skip politely when Postgres is down — but in CI that
# silent skip would produce a green run that tested almost nothing. ci.yml sets REQUIRE_DB=1,
# turning an unreachable DB into a loud collection-time failure instead of 30+ silent skips.
if os.environ.get("REQUIRE_DB") == "1" and not _DB_AVAILABLE:
    raise RuntimeError(
        "REQUIRE_DB=1 but Postgres is unreachable — refusing to run a green-but-empty suite"
    )

requires_db = pytest.mark.skipif(
    not _DB_AVAILABLE, reason="Postgres not reachable (run `make db-up && make seed`)"
)


@pytest.fixture
def db_session():
    with SessionLocal() as session:
        yield session


class FakeRedis:
    """Minimal stand-in implementing the subset the three caches use (M1: mget/set/pipeline;
    M3 added get/setex/delete/incrby/ttl/scan_iter). TTLs are recorded, never enforced —
    expiry behavior is Redis's job, tests only assert we SET one."""

    def __init__(self) -> None:
        self.store: dict[bytes, bytes] = {}
        self.ttls: dict[bytes, int] = {}

    @staticmethod
    def _b(key) -> bytes:
        return key.encode() if isinstance(key, str) else key

    def get(self, key):
        return self.store.get(self._b(key))

    def mget(self, keys):
        return [self.store.get(self._b(k)) for k in keys]

    def set(self, key, value):
        self.store[self._b(key)] = value

    def setex(self, key, ttl, value):
        key = self._b(key)
        self.store[key] = value
        self.ttls[key] = int(ttl)

    def delete(self, *keys):
        removed = 0
        for k in keys:
            k = self._b(k)
            if k in self.store:
                del self.store[k]
                self.ttls.pop(k, None)
                removed += 1
        return removed

    def incrby(self, key, amount=1):
        key = self._b(key)
        value = int(self.store.get(key, b"0")) + int(amount)
        self.store[key] = str(value).encode()
        return value

    def incr(self, key):
        return self.incrby(key, 1)

    def incrbyfloat(self, key, amount):  # M6: the per-session cost accumulator (ADR-045)
        key = self._b(key)
        value = float(self.store.get(key, b"0")) + float(amount)
        self.store[key] = repr(value).encode()
        return value

    def expire(self, key, ttl):
        self.ttls[self._b(key)] = int(ttl)
        return True

    def ttl(self, key):
        return self.ttls.get(self._b(key), -1)

    def scan_iter(self, match=None):
        pattern = self._b(match).decode() if match is not None else "*"
        yield from (k for k in list(self.store) if fnmatch(k.decode(), pattern))

    def keys(self, pattern=b"*"):
        return list(self.scan_iter(match=pattern))

    def pipeline(self, transaction=True):  # noqa: ARG002 — signature parity with redis-py
        return _FakePipeline(self)

    def ping(self):
        return True


class _FakePipeline:
    """Buffers any FakeRedis method call, replays on execute() (enough for cache writes)."""

    def __init__(self, parent: FakeRedis) -> None:
        self.parent = parent
        self.ops: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def buffer(*args):
            self.ops.append((name, args))
            return self

        return buffer

    def execute(self):
        return [getattr(self.parent, name)(*args) for name, args in self.ops]


class DownRedis:
    """Every operation raises redis.RedisError — for 'cache down => request proceeds' tests."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise redis.RedisError("redis is down (simulated)")

        return boom


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def down_redis() -> DownRedis:
    return DownRedis()


@pytest.fixture(autouse=True)
def response_cache_redis(monkeypatch) -> FakeRedis:
    """Hermetic per-test Redis for the response-cache decorator (M3): the decorated tools
    (get_user_assets, list_catalog_items) run inside many tool tests, and must neither read
    a live Redis (stale cross-test hits) nor leak 5-minute entries into one."""
    fake = FakeRedis()
    monkeypatch.setattr("app.cache.response_cache.get_redis", lambda: fake)
    return fake

"""Shared cache hit/miss counters: one tiny helper used by all three caches.

Counters are persistent Redis INCRs (`cache:stats:{name}:{hit|miss}`) so they survive process
restarts and aggregate across processes (API workers + ingest runs). `GET /cache/stats` reads
them back with hit rates. This is the surface M6 later exports to Langfuse — keep it boring.

Counter writes must NEVER fail a request (same RedisError tolerance as the M1 embedding cache):
a metric about an optimization is even less of a dependency than the optimization itself.
"""
# Implemented in M3.

from __future__ import annotations

import logging

import redis

from app.cache.redis_client import get_redis

logger = logging.getLogger(__name__)

# The three caches (ADR-006 embedding, ADR-023 semantic, ADR-025 response); /cache/stats
# reports exactly these so a cache that never fires still shows up as zeros.
CACHE_NAMES = ("embedding", "semantic", "response")


def _key(name: str, outcome: str) -> bytes:
    return f"cache:stats:{name}:{outcome}".encode()


def record(name: str, *, hits: int = 0, misses: int = 0, r: redis.Redis | None = None) -> None:
    """Bump the persistent hit/miss counters for one cache; silently a no-op if Redis is down."""
    if not hits and not misses:
        return
    try:
        r = r if r is not None else get_redis()
        pipe = r.pipeline(transaction=False)
        if hits:
            pipe.incrby(_key(name, "hit"), hits)
        if misses:
            pipe.incrby(_key(name, "miss"), misses)
        pipe.execute()
    except redis.RedisError as exc:
        logger.warning("cache stats write failed for %s (%s); continuing", name, exc)


def snapshot(r: redis.Redis | None = None) -> dict:
    """Counts + hit rates for every cache. Raises redis.RedisError if Redis is unreachable —
    the /cache/stats route turns that into an explicit 'unavailable' payload (a stats read has
    no request to protect)."""
    r = r if r is not None else get_redis()
    keys = [_key(name, outcome) for name in CACHE_NAMES for outcome in ("hit", "miss")]
    values = r.mget(keys)
    counts = [int(v) if v is not None else 0 for v in values]
    result: dict[str, dict] = {}
    for i, name in enumerate(CACHE_NAMES):
        hits, misses = counts[2 * i], counts[2 * i + 1]
        total = hits + misses
        result[name] = {
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 4) if total else None,
        }
    return result

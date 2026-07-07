"""Shared Redis connection factory."""
# Implemented in M1 (pulled forward from M3 together with the embedding cache — see CLAUDE.md).

from __future__ import annotations

import redis

from app.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Process-wide Redis client (redis-py pools connections internally).

    decode_responses stays False: the embedding cache stores packed float32 bytes, not text.
    """
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis_url, decode_responses=False)
    return _client

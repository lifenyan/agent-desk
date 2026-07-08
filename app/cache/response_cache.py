"""Response cache: TTL cache decorator for cheap, slow-changing read tools (ADR-025).

Applied to the PLAIN functions (list_catalog_items, get_user_assets) BEFORE their
`function_tool` wrapping, so the SDK wrappers pick it up for free — `functools.wraps`
preserves the signature/docstring the SDK derives its schema from (verified against SDK
0.17.7, including StrEnum args and the RunContextWrapper ctx param).

Key discipline: the key function receives the tool's own arguments and must fold in the
acting user where the read is user-scoped (get_user_assets keys on the trusted
ctx.context.user_id — identity NEVER comes from an LLM argument, see user_tools DESIGN NOTE).
Returning None from the key function means "don't cache this call" (e.g. no acting user).
Error dicts are never stored: a typo'd enum or missing user must not become a 5-minute-sticky
answer, and the SDK error-feedback loop needs fresh evaluations to let the model self-correct.

No write-side invalidation yet, on purpose: nothing in-app mutates assets or catalog items in
M3 (orders reference catalog rows, never change them). The trigger to add it: any tool or
admin surface that writes those tables — then the writer must delete the matching keys, same
pattern as ingest -> semantic cache (ADR-024).

Like every cache here: Redis down => call straight through.
"""
# Implemented in M3.

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable

import redis

from app.cache import stats
from app.cache.redis_client import get_redis
from app.config import get_settings

logger = logging.getLogger(__name__)


def cache_response(key_fn: Callable[..., str | None], ttl_seconds: int | None = None):
    """Cache a read tool's dict result in Redis under `resp:{fn name}:{key_fn(*args)}`.

    key_fn gets the wrapped function's exact arguments and returns the cache-key suffix,
    or None to bypass caching for that call.
    """

    def decorator(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> dict:
            # A/B seam (M6, ADR-044): deliberate off — straight through, no stats, no noise.
            if get_settings().caches_disabled:
                return fn(*args, **kwargs)
            suffix = key_fn(*args, **kwargs)
            if suffix is None:
                return fn(*args, **kwargs)
            key = f"resp:{fn.__name__}:{suffix}".encode()
            r = get_redis()
            try:
                blob = r.get(key)
            except redis.RedisError as exc:
                logger.warning("response cache unavailable (%s); calling through", exc)
                return fn(*args, **kwargs)
            if blob is not None:
                stats.record("response", hits=1, r=r)
                return json.loads(blob)
            stats.record("response", misses=1, r=r)
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and "error" not in result:
                ttl = (
                    ttl_seconds
                    if ttl_seconds is not None
                    else get_settings().response_cache_ttl_seconds
                )
                try:
                    r.setex(key, ttl, json.dumps(result).encode())
                except redis.RedisError as exc:
                    logger.warning("response cache write failed (%s); continuing", exc)
            return result

        return wrapper

    return decorator

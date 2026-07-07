"""Embedding cache: sha256(model + text) -> vector, avoids re-embedding identical text.

Pulled forward from M3 (see CLAUDE.md): the M1 eval-tuning loop re-ingests all chunks on every
chunking/fusion iteration, and without this layer each iteration would re-pay the embedding API
for ~400 unchanged chunks.

Design (ADR-006): exact-match only, keyed `emb:sha256(model \\x00 text)`, NO TTL — an embedding
of a given (model, text) pair is immutable, so entries can never go stale. Values are packed
float32 (pgvector's own precision), 6 KB per vector instead of ~30 KB of JSON.

This is the layer callers use (`get_or_embed`); rag/embeddings.py underneath is cache-oblivious.
"""
# Implemented in M1 (pulled forward from M3). M3 adds the semantic + response caches.

from __future__ import annotations

import hashlib
import logging
import struct

import redis

from app.cache.redis_client import get_redis
from app.config import get_settings
from app.rag.embeddings import embed_texts

logger = logging.getLogger(__name__)


def _key(model: str, text: str) -> bytes:
    digest = hashlib.sha256(model.encode() + b"\x00" + text.encode()).hexdigest()
    return f"emb:{digest}".encode()


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def get_or_embed(
    texts: list[str],
    model: str | None = None,
    r: redis.Redis | None = None,
) -> list[list[float]]:
    """Return embeddings for `texts` (in order), embedding only cache misses.

    Duplicate texts within one call are embedded once. If Redis is down, degrades to
    uncached embedding with a warning — the cache is an optimization, never a dependency.
    """
    if not texts:
        return []
    model = model or get_settings().embedding_model
    r = r if r is not None else get_redis()

    keys = [_key(model, t) for t in texts]
    try:
        cached = r.mget(keys)
    except redis.RedisError as exc:
        logger.warning("embedding cache unavailable (%s); embedding uncached", exc)
        return embed_texts(texts, model=model)

    # Deduplicate misses so identical texts cost one embedding.
    miss_texts: dict[bytes, str] = {
        k: t for k, t, blob in zip(keys, texts, cached) if blob is None
    }
    if miss_texts:
        miss_keys = list(miss_texts)
        vectors = embed_texts(list(miss_texts.values()), model=model)
        fresh = dict(zip(miss_keys, vectors))
        try:
            pipe = r.pipeline(transaction=False)
            for k, v in fresh.items():
                pipe.set(k, _pack(v))  # no TTL: (model, text) -> vector is immutable
            pipe.execute()
        except redis.RedisError as exc:
            logger.warning("embedding cache write failed (%s); continuing", exc)
    else:
        fresh = {}

    logger.info(
        "embedding cache: %d hits, %d embedded", len(texts) - len(miss_texts), len(miss_texts)
    )
    return [fresh[k] if blob is None else _unpack(blob) for k, blob in zip(keys, cached)]


def embed_query(text: str, model: str | None = None) -> list[float]:
    """Convenience wrapper for the single-query path (search, evals)."""
    return get_or_embed([text], model=model)[0]

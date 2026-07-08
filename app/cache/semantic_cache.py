"""Semantic cache: similarity-matched query -> stored knowledge answer, checked BEFORE any agent.

This implements the last architecture invariant (ADR-023): the cache runs before any agent and
serves READ-ONLY intents only — a cached answer must never place an order or file a ticket.
That guarantee is enforced at WRITE time, not read time: `is_cacheable` only admits runs whose
final agent was `knowledge` with real evidence (citations + the ADR-017 "Sources:" contract), so
nothing action-shaped or refusal-shaped is ever stored, and a >threshold-similar query to a
stored knowledge Q is itself a knowledge Q — no pre-agent LLM classifier needed.

Lookup is brute-force cosine over every cached entry in app code: the stock redis:7-alpine image
carries no vector module (RediSearch), and at demo scale (bounded by 24 h TTL x distinct
first-turn knowledge questions) a full scan is microseconds. Revisit trigger: entry count in the
thousands or a Redis image change — then move to RediSearch/pgvector for the lookup.

Entries are GLOBAL across users — lookup keys on similarity only. Safe today because knowledge
answers are user-independent (knowledge tools read only the KB, never user data); revisit
trigger: any user-scoped tool landing on the knowledge agent.

Consistency: entries carry `cited_article_ids` so ingest (the single article write path) can
delete exactly the entries whose sources changed — see `invalidate_articles` + ADR-024.

Like every cache here (M1 rule): an optimization, never a dependency — Redis down means the
lookup silently misses and the request proceeds to the agents.
"""
# Implemented in M3 (ADR-023/024). The routes_chat pre-check + session policy live in routes_chat.

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import redis

from app.cache import stats
from app.cache.embedding_cache import _pack, _unpack, embed_query
from app.cache.redis_client import get_redis
from app.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = b"semcache:"


def _key(model: str, query: str) -> bytes:
    """Exact-duplicate queries overwrite their entry (sha of model+query); similar-but-distinct
    queries each get their own entry — dedup beyond exact match is the cosine lookup's job."""
    digest = hashlib.sha256(model.encode() + b"\x00" + query.encode()).hexdigest()
    return _PREFIX + digest.encode()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class CachedAnswer:
    """A semantic-cache hit, ready to be returned as a ChatResponse (with cached=True)."""

    query: str  # the ORIGINAL stored query (visible for debugging/demo, not shown to users)
    answer: str
    citations: list[dict]  # stored as-is; routes_chat must NOT re-run _collect_citations
    similarity: float


def is_cacheable(agent_name: str, answer: str, citations: list[dict]) -> bool:
    """Write-time read-only classification (ADR-023).

    Store ONLY runs that ended on the knowledge agent (never fulfillment/incident — their
    answers describe side effects that must re-execute) AND that actually answered: per the
    ADR-017 output contract, answers end with a "Sources:" list and carry citations; refusals
    never do. Error paths fail both checks. Belt and suspenders on purpose — either signal
    alone would already exclude refusals.
    """
    return agent_name == "knowledge" and bool(citations) and "Sources:" in answer


def lookup(
    message: str,
    *,
    r: redis.Redis | None = None,
    threshold: float | None = None,
) -> CachedAnswer | None:
    """Best cosine match above threshold among stored entries, or None.

    Scans Redis BEFORE embedding: if Redis is down or the cache is empty there is nothing to
    match, so the embedding call (the only part that costs money) is skipped entirely.
    """
    settings = get_settings()
    # A/B seam (M6, ADR-044): deliberate off => no lookup, no stats bump, silent. The e2e
    # knowledge_cache flow's cached=true assertion WILL fail under this flag — expected: the
    # OFF arm measures exactly what that assertion normally proves.
    if settings.caches_disabled:
        return None
    r = r if r is not None else get_redis()
    threshold = threshold if threshold is not None else settings.semantic_cache_threshold
    start = time.perf_counter()
    try:
        keys = list(r.scan_iter(match=_PREFIX + b"*"))
        blobs = r.mget(keys) if keys else []
    except redis.RedisError as exc:
        logger.warning("semantic cache unavailable (%s); skipping lookup", exc)
        return None

    best: dict | None = None
    best_sim = -1.0
    if keys:
        query_vec = embed_query(message)
        for blob in blobs:
            if blob is None:  # expired between SCAN and MGET
                continue
            entry = json.loads(blob)
            sim = _cosine(query_vec, _unpack(base64.b64decode(entry["embedding"])))
            if sim > best_sim:
                best_sim, best = sim, entry

    elapsed_ms = (time.perf_counter() - start) * 1000
    if best is not None and best_sim > threshold:
        stats.record("semantic", hits=1, r=r)
        logger.info(
            "semantic cache HIT sim=%.3f (threshold %.2f) in %.1f ms: %r ~ %r",
            best_sim,
            threshold,
            elapsed_ms,
            message,
            best["query"],
        )
        return CachedAnswer(
            query=best["query"],
            answer=best["answer"],
            citations=best["citations"],
            similarity=best_sim,
        )
    stats.record("semantic", misses=1, r=r)
    logger.info(
        "semantic cache miss (best sim %.3f over %d entries) in %.1f ms",
        best_sim,
        len(keys),
        elapsed_ms,
    )
    return None


def store(
    message: str,
    answer: str,
    citations: list[dict],
    *,
    r: redis.Redis | None = None,
) -> bool:
    """Store one CACHEABLE result (caller gates via is_cacheable), TTL 24 h.

    The embedding call is effectively free here: routes_chat only stores after a lookup miss,
    and that lookup already routed the same text through the embedding cache.
    """
    settings = get_settings()
    if settings.caches_disabled:  # A/B seam (M6, ADR-044): the OFF arm must not warm the cache
        return False
    r = r if r is not None else get_redis()
    entry = {
        "query": message,
        "embedding": base64.b64encode(_pack(embed_query(message))).decode(),
        "answer": answer,
        "citations": citations,
        "cited_article_ids": [c["article_id"] for c in citations],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r.setex(
            _key(settings.embedding_model, message),
            settings.semantic_cache_ttl_seconds,
            json.dumps(entry).encode(),
        )
        return True
    except redis.RedisError as exc:
        logger.warning("semantic cache write failed (%s); continuing", exc)
        return False


def invalidate_articles(changed_article_ids: set[str], *, r: redis.Redis | None = None) -> int:
    """Delete every entry citing any changed article; returns how many were deleted.

    Called from the single article write path (app/rag/ingest.py) with the content-hash diff
    (ADR-024). Targeted on purpose: answers know their sources (cited_article_ids), so an
    edit to one article must not flush unrelated cached answers.
    """
    if not changed_article_ids:
        return 0
    changed = {str(a) for a in changed_article_ids}
    r = r if r is not None else get_redis()
    try:
        keys = list(r.scan_iter(match=_PREFIX + b"*"))
        blobs = r.mget(keys) if keys else []
        stale = [
            k
            for k, blob in zip(keys, blobs)
            if blob is not None and changed & set(json.loads(blob)["cited_article_ids"])
        ]
        if stale:
            r.delete(*stale)
        logger.info(
            "semantic cache invalidation: %d/%d entries cited a changed article",
            len(stale),
            len(keys),
        )
        return len(stale)
    except redis.RedisError as exc:
        logger.warning("semantic cache invalidation failed (%s); entries expire by TTL", exc)
        return 0

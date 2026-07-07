"""Embedding client (via LiteLLM), wrapped by the Redis embedding cache.

Thin and deterministic: no caching here (that's cache/embedding_cache.py, the layer everything
else calls), just batching + retry around the provider API. The model must produce vectors of
models.EMBED_DIM (1536) — checked at runtime because a silent dim mismatch would corrupt the
HNSW index rather than error.
"""
# Implemented in M1.

from __future__ import annotations

import logging
import time

import litellm

from app.config import get_settings
from app.db.models import EMBED_DIM

logger = logging.getLogger(__name__)

_BATCH_SIZE = 256  # OpenAI embedding API caps input arrays well above this; keeps requests small
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.0


def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed `texts` in order. Raises after _MAX_ATTEMPTS on persistent provider failure."""
    if not texts:
        return []
    model = model or get_settings().embedding_model

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start : start + _BATCH_SIZE]
        vectors.extend(_embed_batch_with_retry(batch, model))

    for v in vectors:
        if len(v) != EMBED_DIM:
            raise ValueError(
                f"model {model!r} returned {len(v)}-dim vectors, schema expects {EMBED_DIM}"
            )
    return vectors


def _embed_batch_with_retry(batch: list[str], model: str) -> list[list[float]]:
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = litellm.embedding(model=model, input=batch)
            # Provider may reorder; restore input order via the index field.
            data = sorted(response.data, key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except Exception as exc:  # noqa: BLE001 — LiteLLM raises provider-specific subclasses
            last_exc = exc
            if attempt == _MAX_ATTEMPTS:
                break
            delay = _BACKOFF_BASE_S * 2 ** (attempt - 1)
            logger.warning(
                "embedding attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                _MAX_ATTEMPTS,
                exc.__class__.__name__,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"embedding failed after {_MAX_ATTEMPTS} attempts") from last_exc

"""Unit tests for embedding/semantic/response caches (hits, misses, TTL, invalidation, read-only gating)."""
# M1: embedding-cache tests (pulled forward with the cache). TODO(M3): semantic + response cache tests.

from __future__ import annotations

import pytest

from app.cache import embedding_cache
from app.cache.embedding_cache import _key, _pack, _unpack, get_or_embed
from app.db.models import EMBED_DIM


@pytest.fixture
def fake_embedder(monkeypatch):
    """Deterministic embed_texts stand-in that counts how many texts were actually embedded."""
    calls: list[list[str]] = []

    def _fake(texts, model=None):  # noqa: ARG001
        calls.append(list(texts))
        return [[float(len(t))] * EMBED_DIM for t in texts]

    monkeypatch.setattr(embedding_cache, "embed_texts", _fake)
    return calls


def test_pack_roundtrip_is_float32_exact():
    vec = [0.1, -2.5, 3.25, 0.0]
    assert _unpack(_pack(vec)) == pytest.approx(vec, abs=1e-7)


def test_key_depends_on_model_and_text():
    assert _key("m1", "hello") != _key("m2", "hello")
    assert _key("m1", "hello") != _key("m1", "world")
    # delimiter prevents ("ab","c") colliding with ("a","bc")
    assert _key("ab", "c") != _key("a", "bc")


def test_first_call_embeds_second_call_hits(fake_redis, fake_embedder):
    texts = ["alpha", "beta"]
    first = get_or_embed(texts, model="m", r=fake_redis)
    assert fake_embedder == [["alpha", "beta"]]
    second = get_or_embed(texts, model="m", r=fake_redis)
    assert fake_embedder == [["alpha", "beta"]]  # no new embedding work
    assert first == second


def test_duplicates_within_one_call_embed_once(fake_redis, fake_embedder):
    result = get_or_embed(["same", "same", "same"], model="m", r=fake_redis)
    assert fake_embedder == [["same"]]
    assert result[0] == result[1] == result[2]


def test_partial_hit_only_embeds_misses(fake_redis, fake_embedder):
    get_or_embed(["a"], model="m", r=fake_redis)
    get_or_embed(["a", "b"], model="m", r=fake_redis)
    assert fake_embedder == [["a"], ["b"]]


def test_model_is_part_of_the_key(fake_redis, fake_embedder):
    get_or_embed(["x"], model="m1", r=fake_redis)
    get_or_embed(["x"], model="m2", r=fake_redis)
    assert len(fake_embedder) == 2


def test_no_ttl_set(fake_redis, fake_embedder):
    get_or_embed(["x"], model="m", r=fake_redis)
    # FakeRedis.set has no TTL parameters — reaching here without TypeError proves plain SET;
    # the value is retrievable and stable.
    assert len(fake_redis.store) == 1

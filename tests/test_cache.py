"""Unit tests for embedding/semantic/response caches (hits, misses, TTL, invalidation, read-only gating).

All LLM-free: embeddings are faked (deterministic vectors), Redis is the conftest FakeRedis.
The one DB-backed test (two users don't share a get_user_assets entry) skips when Postgres is
down, per the test_tools.py precedent.
"""
# M1: embedding-cache tests (pulled forward with the cache). M3: semantic + response cache +
# counters + invalidation tests (ADR-023/024/025).

from __future__ import annotations

import uuid

import pytest
from dotenv import load_dotenv
from sqlalchemy import select

from tests.conftest import requires_db

load_dotenv()

from agents import RunContextWrapper, SQLiteSession  # noqa: E402

from app.agents.context import ChatContext  # noqa: E402
from app.api.routes_chat import _is_first_turn  # noqa: E402
from app.cache import embedding_cache, response_cache, semantic_cache, stats  # noqa: E402
from app.cache.embedding_cache import _key, _pack, _unpack, get_or_embed  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.db.models import EMBED_DIM, User  # noqa: E402
from app.rag.ingest import changed_article_ids, content_hash  # noqa: E402
from app.tools.user_tools import get_user_assets  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Embedding cache (M1)
# ---------------------------------------------------------------------------------------------


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
    emb_keys = [k for k in fake_redis.store if k.startswith(b"emb:")]
    assert len(emb_keys) == 1
    assert emb_keys[0] not in fake_redis.ttls  # plain SET: (model, text) -> vector is immutable


def test_embedding_counters_tick(fake_redis, fake_embedder):
    get_or_embed(["a", "b"], model="m", r=fake_redis)  # 2 misses
    get_or_embed(["a"], model="m", r=fake_redis)  # 1 hit
    assert fake_redis.store[b"cache:stats:embedding:miss"] == b"2"
    assert fake_redis.store[b"cache:stats:embedding:hit"] == b"1"


# ---------------------------------------------------------------------------------------------
# Counters (M3): shared helper + snapshot
# ---------------------------------------------------------------------------------------------


def test_stats_snapshot_counts_and_rates(fake_redis):
    stats.record("semantic", hits=3, misses=1, r=fake_redis)
    snap = stats.snapshot(r=fake_redis)
    assert snap["semantic"] == {"hits": 3, "misses": 1, "hit_rate": 0.75}
    # caches that never fired still appear, with a null rate (no division by zero)
    assert snap["response"] == {"hits": 0, "misses": 0, "hit_rate": None}


def test_stats_write_never_raises_when_redis_down(down_redis):
    stats.record("semantic", hits=1, r=down_redis)  # must not raise


# ---------------------------------------------------------------------------------------------
# Semantic cache (M3, ADR-023): fake unit vectors, real cosine/gating/TTL/invalidation logic
# ---------------------------------------------------------------------------------------------

ANSWER = "Open Settings > Security and follow the reset flow.\n\nSources:\n- Password Reset Guide"
CITES = [{"article_id": "art-pw", "title": "Password Reset Guide", "rrf_score": 0.033}]


@pytest.fixture
def query_vectors(monkeypatch):
    """text -> vector lookup table; a query missing from it that gets embedded is a test bug."""
    vectors: dict[str, list[float]] = {}
    monkeypatch.setattr(
        semantic_cache,
        "embed_query",
        lambda text, model=None: vectors[text],  # noqa: ARG005
    )
    return vectors


def test_semantic_hit_above_threshold(fake_redis, query_vectors):
    query_vectors["how do I reset my password?"] = [1.0, 0.0, 0.0]
    query_vectors["password reset — how?"] = [0.999, 0.0447, 0.0]  # cosine ~0.999
    assert semantic_cache.store("how do I reset my password?", ANSWER, CITES, r=fake_redis)

    hit = semantic_cache.lookup("password reset — how?", r=fake_redis, threshold=0.95)
    assert hit is not None
    assert hit.answer == ANSWER
    assert hit.citations == CITES  # stored citations verbatim, not re-collected
    assert hit.similarity > 0.95


def test_semantic_miss_below_threshold(fake_redis, query_vectors):
    query_vectors["how do I reset my password?"] = [1.0, 0.0, 0.0]
    query_vectors["order me a laptop"] = [0.0, 1.0, 0.0]  # orthogonal
    semantic_cache.store("how do I reset my password?", ANSWER, CITES, r=fake_redis)

    assert semantic_cache.lookup("order me a laptop", r=fake_redis, threshold=0.95) is None


def test_semantic_counters_tick(fake_redis, query_vectors):
    query_vectors["q"] = [1.0, 0.0]
    query_vectors["q2"] = [0.0, 1.0]
    semantic_cache.store("q", ANSWER, CITES, r=fake_redis)
    semantic_cache.lookup("q", r=fake_redis, threshold=0.95)  # hit (identical vector)
    semantic_cache.lookup("q2", r=fake_redis, threshold=0.95)  # miss
    assert fake_redis.store[b"cache:stats:semantic:hit"] == b"1"
    assert fake_redis.store[b"cache:stats:semantic:miss"] == b"1"


def test_semantic_entries_get_the_24h_ttl(fake_redis, query_vectors):
    query_vectors["q"] = [1.0, 0.0]
    semantic_cache.store("q", ANSWER, CITES, r=fake_redis)
    sem_keys = [k for k in fake_redis.store if k.startswith(b"semcache:")]
    assert len(sem_keys) == 1
    assert fake_redis.ttls[sem_keys[0]] == get_settings().semantic_cache_ttl_seconds


def test_empty_cache_skips_the_embedding_call(fake_redis, query_vectors):
    # query_vectors is empty: if lookup embedded the query it would KeyError. It must not —
    # with nothing to match, the (only paid) embedding call is skipped entirely.
    assert semantic_cache.lookup("anything", r=fake_redis, threshold=0.95) is None


def test_write_time_read_only_gate():
    """The invariant: nothing action-shaped or refusal-shaped is ever STORED (ADR-023)."""
    # fulfillment/incident runs are never cacheable — however answer-like they look.
    assert not semantic_cache.is_cacheable("fulfillment", "Order placed! Sources:", CITES)
    assert not semantic_cache.is_cacheable("incident", "Ticket created. Sources:", CITES)
    assert not semantic_cache.is_cacheable("router", ANSWER, CITES)
    # refusals: never carry "Sources:" and have zero citations (ADR-017 contract) — either
    # signal alone must block the store.
    refusal = "I couldn't find coverage for that. Would you like me to open a ticket?"
    assert not semantic_cache.is_cacheable("knowledge", refusal, [])
    assert not semantic_cache.is_cacheable("knowledge", "answer without contract", CITES)
    assert not semantic_cache.is_cacheable("knowledge", ANSWER, [])
    # the one storable shape: knowledge + Sources: + citations.
    assert semantic_cache.is_cacheable("knowledge", ANSWER, CITES)


async def test_mid_conversation_turns_bypass_the_cache(tmp_path):
    """ADR-023 session policy: only a conversation's FIRST message may consult the cache."""
    assert await _is_first_turn(None)  # no session = one-shot = fresh
    session = SQLiteSession("t-1", tmp_path / "sessions.sqlite3")
    assert await _is_first_turn(session)  # fresh conversation
    await session.add_items([{"role": "user", "content": "how do I reset my password?"}])
    assert not await _is_first_turn(session)  # "yes, go ahead" must reach the agents


def test_invalidation_deletes_exactly_the_citing_entries(fake_redis, query_vectors):
    query_vectors["pw question"] = [1.0, 0.0, 0.0]
    query_vectors["vpn question"] = [0.0, 1.0, 0.0]
    semantic_cache.store("pw question", ANSWER, CITES, r=fake_redis)
    semantic_cache.store(
        "vpn question",
        "Use the corp VPN app.\n\nSources:\n- VPN Guide",
        [{"article_id": "art-vpn", "title": "VPN Guide", "rrf_score": 0.03}],
        r=fake_redis,
    )

    assert semantic_cache.invalidate_articles({"art-pw"}, r=fake_redis) == 1

    assert semantic_cache.lookup("pw question", r=fake_redis, threshold=0.95) is None
    vpn_hit = semantic_cache.lookup("vpn question", r=fake_redis, threshold=0.95)
    assert vpn_hit is not None and vpn_hit.citations[0]["article_id"] == "art-vpn"


def test_invalidation_with_no_changes_is_a_noop(fake_redis, query_vectors):
    query_vectors["q"] = [1.0, 0.0]
    semantic_cache.store("q", ANSWER, CITES, r=fake_redis)
    assert semantic_cache.invalidate_articles(set(), r=fake_redis) == 0
    assert semantic_cache.invalidate_articles({"art-other"}, r=fake_redis) == 0
    assert semantic_cache.lookup("q", r=fake_redis, threshold=0.95) is not None


def test_semantic_cache_redis_down_degrades_silently(down_redis, query_vectors):
    assert semantic_cache.lookup("anything", r=down_redis) is None  # request proceeds to agents
    query_vectors["q"] = [1.0, 0.0]
    assert semantic_cache.store("q", ANSWER, CITES, r=down_redis) is False
    assert semantic_cache.invalidate_articles({"art-pw"}, r=down_redis) == 0


# ---------------------------------------------------------------------------------------------
# Ingest change detection (M3, ADR-024): pure hash/diff helpers
# ---------------------------------------------------------------------------------------------


def test_content_hash_is_order_and_boundary_sensitive():
    assert content_hash(["ab", "c"]) != content_hash(["a", "bc"])  # delimiter
    assert content_hash(["a", "b"]) != content_hash(["b", "a"])  # chunk order matters
    assert content_hash(["a", "b"]) == content_hash(["a", "b"])


def test_changed_article_ids_diff_semantics():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    old = {a: "h-a", b: "h-b", c: "h-c"}
    new = {a: "h-a", b: "h-b-EDITED", d: "h-d"}  # b edited, c deleted, d brand new
    # edited + deleted articles invalidate; brand-new ones can't be cited by any cached answer
    assert changed_article_ids(old, new) == {str(b), str(c)}


# ---------------------------------------------------------------------------------------------
# Response cache (M3, ADR-025): decorator behavior + the real tool wiring
# ---------------------------------------------------------------------------------------------


def _counting(result_fn):
    calls = []

    def fn(arg):
        calls.append(arg)
        return result_fn(arg)

    return fn, calls


def test_response_cache_second_call_skips_the_function(response_cache_redis):
    fn, calls = _counting(lambda arg: {"value": arg})
    cached = response_cache.cache_response(key_fn=lambda arg: str(arg))(fn)
    assert cached("x") == {"value": "x"}
    assert cached("x") == {"value": "x"}
    assert calls == ["x"]  # second call served from Redis
    key = b"resp:fn:x"
    assert response_cache_redis.ttls[key] == get_settings().response_cache_ttl_seconds


def test_response_cache_key_separates_users(response_cache_redis):
    fn, calls = _counting(lambda user: {"assets": [f"laptop-of-{user}"]})
    cached = response_cache.cache_response(key_fn=lambda user: user)(fn)
    assert cached("alice")["assets"] == ["laptop-of-alice"]
    assert cached("bob")["assets"] == ["laptop-of-bob"]  # not alice's entry
    assert cached("alice")["assets"] == ["laptop-of-alice"]
    assert calls == ["alice", "bob"]


def test_response_cache_never_stores_error_dicts(response_cache_redis):
    fn, calls = _counting(lambda arg: {"error": "no such thing"})
    cached = response_cache.cache_response(key_fn=lambda arg: str(arg))(fn)
    cached("x")
    cached("x")
    assert calls == ["x", "x"]  # error re-evaluated every time (SDK self-correction loop)
    assert not [k for k in response_cache_redis.store if k.startswith(b"resp:")]


def test_response_cache_key_fn_none_bypasses(response_cache_redis):
    fn, calls = _counting(lambda arg: {"value": arg})
    cached = response_cache.cache_response(key_fn=lambda arg: None)(fn)
    cached("x")
    cached("x")
    assert calls == ["x", "x"]
    assert not [k for k in response_cache_redis.store if k.startswith(b"resp:")]


def test_response_cache_redis_down_calls_through(monkeypatch, down_redis):
    monkeypatch.setattr(response_cache, "get_redis", lambda: down_redis)
    fn, calls = _counting(lambda arg: {"value": arg})
    cached = response_cache.cache_response(key_fn=lambda arg: str(arg))(fn)
    assert cached("x") == {"value": "x"}
    assert cached("x") == {"value": "x"}
    assert calls == ["x", "x"]


def test_response_counters_tick(response_cache_redis):
    fn, _ = _counting(lambda arg: {"value": arg})
    cached = response_cache.cache_response(key_fn=lambda arg: str(arg))(fn)
    cached("x")  # miss
    cached("x")  # hit
    assert response_cache_redis.store[b"cache:stats:response:miss"] == b"1"
    assert response_cache_redis.store[b"cache:stats:response:hit"] == b"1"


def test_get_user_assets_without_user_bypasses_cache(response_cache_redis):
    """The no-acting-user error path must stay uncached AND unkeyed (key_fn returns None)."""
    ctx = RunContextWrapper(context=ChatContext(user_id=None))
    assert "error" in get_user_assets(ctx)
    assert not [k for k in response_cache_redis.store if k.startswith(b"resp:")]


@requires_db
def test_get_user_assets_not_shared_between_users(response_cache_redis):
    """The REAL wiring: the decorator keys on the trusted ctx identity, one entry per user."""
    with SessionLocal() as s:
        u1, u2 = s.scalars(select(User.email).order_by(User.email).limit(2)).all()
    get_user_assets(RunContextWrapper(context=ChatContext(user_id=u1)))
    get_user_assets(RunContextWrapper(context=ChatContext(user_id=u2)))
    keys = [k for k in response_cache_redis.store if k.startswith(b"resp:get_user_assets:")]
    assert len(keys) == 2  # distinct entries — no cross-user sharing
    assert {k.decode().rsplit(":", 1)[-1] for k in keys} == {u1, u2}

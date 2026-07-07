"""Unit tests for session store and user_facts extraction/dedup — LLM-free by design.

The Postgres session store (ADR-030) is exercised through the real DB (requires_db, like the
tool tests): round-trip, isolation between session_ids, and survival across store instances
(the unit-level equivalent of an API restart — nothing is held in process state). The fact
extractor's LLM call is FAKED (monkeypatched Runner.run): what these tests pin is the
plumbing — injection formatting/threshold and the deterministic merge rule (ADR-031).
"""
# Implemented in M5.

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text

from app.api.routes_chat import _is_first_turn, _load_session
from app.db.database import SessionLocal
from app.db.models import User, UserFact
from app.memory import extraction
from app.memory.extraction import ExtractedFact, ExtractionResult, extract_and_store
from app.memory.session_store import get_session_store
from app.memory.user_facts import FactCandidate, apply_extracted_facts, injection_message
from tests.conftest import requires_db

SEEDED_FACTS_USER = "demo.user@corp.com"  # 3 facts seeded from data/user_facts.json


@pytest.fixture
def session_id():
    """Fresh session id; drops its agent_sessions row (messages cascade) after the test."""
    sid = f"test-{uuid.uuid4()}"
    yield sid
    with SessionLocal() as s:
        s.execute(text("DELETE FROM agent_sessions WHERE session_id = :sid"), {"sid": sid})
        s.commit()


@pytest.fixture
def factless_user():
    """A seeded user with NO facts — mutation tests never touch the demo user's seeded rows."""
    with SessionLocal() as s:
        user = s.scalar(
            select(User)
            .where(User.email != SEEDED_FACTS_USER)
            .where(~User.id.in_(select(UserFact.user_id)))
            .limit(1)
        )
        assert user is not None, "seed data should contain users without facts"
        email, user_id = user.email, user.id
    yield email
    with SessionLocal() as s:
        s.execute(delete(UserFact).where(UserFact.user_id == user_id))
        s.commit()


# --- session store (ADR-030) ------------------------------------------------------------------


@requires_db
async def test_session_store_roundtrip_and_restart(session_id):
    store = get_session_store(session_id)
    items = [
        {"role": "user", "content": "my VPN keeps dropping"},
        {"role": "assistant", "content": "Try the steps in [VPN troubleshooting]."},
    ]
    await store.add_items(items)
    got = await store.get_items()
    assert [(i["role"], i["content"]) for i in got] == [(i["role"], i["content"]) for i in items]

    # "Restart": a brand-new store instance for the same id must see the same history —
    # nothing lives in process state, which is exactly what the sqlite stopgap couldn't offer.
    reopened = get_session_store(session_id)
    assert len(await reopened.get_items()) == 2


@requires_db
async def test_session_isolation_between_ids(session_id):
    other_id = f"{session_id}-other"
    try:
        await get_session_store(session_id).add_items([{"role": "user", "content": "mine"}])
        assert await get_session_store(other_id).get_items() == []
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM agent_sessions WHERE session_id = :sid"), {"sid": other_id})
            s.commit()


def test_load_session_is_the_postgres_store():
    """The ADR-019 swap point now hands out the Postgres-backed store; None stays one-shot."""
    assert _load_session(None) is None
    session = _load_session("some-conversation")
    assert type(session).__name__ == "SQLAlchemySession"


@requires_db
async def test_semantic_cache_session_policy_against_new_store(session_id):
    """ADR-023's first-turn-only policy and the cache-hit add_items injection must keep
    working against the Postgres store: an empty session is first-turn; after routes_chat
    injects the short-circuited Q/A (dict items, exactly as the cache-hit path does), it isn't."""
    session = _load_session(session_id)
    assert await _is_first_turn(session)
    await session.add_items(
        [
            {"role": "user", "content": "How do I reset my password?"},
            {"role": "assistant", "content": "Use the self-service portal. Sources: ..."},
        ]
    )
    assert not await _is_first_turn(session)
    assert not await _is_first_turn(_load_session(session_id))  # and not from a fresh handle


# --- fact injection (ADR-031) -----------------------------------------------------------------


@requires_db
def test_injection_message_formats_seeded_facts():
    item = injection_message(SEEDED_FACTS_USER)
    assert item["role"] == "system"
    assert "(device_os) Owns a MacBook Pro 16 (macOS)." in item["content"]
    assert "previous conversations" in item["content"]


@requires_db
def test_injection_skips_low_confidence_and_unknown_users(factless_user):
    assert injection_message("nobody@corp.com") is None
    assert injection_message(factless_user) is None  # no facts -> no system item at all
    apply_extracted_facts(factless_user, [FactCandidate("shoe_size", "Wears size 44 shoes.", 0.2)])
    assert injection_message(factless_user) is None  # below INJECTION_MIN_CONFIDENCE
    apply_extracted_facts(factless_user, [FactCandidate("org", "Works in Finance.", 0.9)])
    assert "Works in Finance." in injection_message(factless_user)["content"]


# --- fact merge rule (ADR-031) ----------------------------------------------------------------


def _fact(email: str, fact_type: str) -> UserFact | None:
    with SessionLocal() as s:
        user = s.scalar(select(User).where(User.email == email))
        return s.scalar(
            select(UserFact).where(UserFact.user_id == user.id, UserFact.fact_type == fact_type)
        )


@requires_db
def test_merge_rule_insert_dedup_replace(factless_user):
    # new fact_type inserts
    counts = apply_extracted_facts(
        factless_user, [FactCandidate("device_os", "Uses a Windows 11 laptop.", 0.8)], source="t1"
    )
    assert counts == {"inserted": 1, "updated": 0, "skipped": 0}

    # same type + same normalized text = duplicate, skipped (case/whitespace-insensitive)
    counts = apply_extracted_facts(
        factless_user, [FactCandidate("device_os", "  uses a windows 11 laptop. ", 0.9)]
    )
    assert counts == {"inserted": 0, "updated": 0, "skipped": 1}

    # different text at >= confidence replaces (contradictions replace, never accumulate)
    counts = apply_extracted_facts(
        factless_user, [FactCandidate("device_os", "Switched to a MacBook Air.", 0.9)], source="t2"
    )
    assert counts == {"inserted": 0, "updated": 1, "skipped": 0}
    row = _fact(factless_user, "device_os")
    assert (row.fact, row.confidence, row.source) == ("Switched to a MacBook Air.", 0.9, "t2")

    # different text at LOWER confidence is ignored — hesitation never overwrites belief
    counts = apply_extracted_facts(
        factless_user, [FactCandidate("device_os", "Might be using Linux?", 0.3)]
    )
    assert counts == {"inserted": 0, "updated": 0, "skipped": 1}
    assert _fact(factless_user, "device_os").fact == "Switched to a MacBook Air."


@requires_db
def test_merge_clamps_confidence_and_tolerates_unknown_user(factless_user):
    apply_extracted_facts(factless_user, [FactCandidate("quirk", "Overconfident.", 7.0)])
    assert _fact(factless_user, "quirk").confidence == 1.0  # DB CHECK would reject 7.0
    counts = apply_extracted_facts("nobody@corp.com", [FactCandidate("org", "Ghost.", 0.9)])
    assert counts == {"inserted": 0, "updated": 0, "skipped": 1}


# --- extraction plumbing (faked LLM) ----------------------------------------------------------


class _FakeRunResult:
    def __init__(self, facts: list[ExtractedFact]):
        self.final_output = ExtractionResult(facts=facts)


@requires_db
async def test_extract_and_store_plumbing(monkeypatch, factless_user):
    async def fake_run(agent, prompt, **kwargs):
        assert "The user's message:" in prompt  # existing facts + message ride in the prompt
        return _FakeRunResult(
            [ExtractedFact(fact_type="travel", fact="Travels weekly for work.", confidence=0.7)]
        )

    monkeypatch.setattr(extraction.Runner, "run", fake_run)
    counts = await extract_and_store(factless_user, "fyi I'm on the road every week", "sess-42")
    assert counts == {"inserted": 1, "updated": 0, "skipped": 0}
    assert _fact(factless_user, "travel").source == "extracted:sess-42"


async def test_extract_skips_without_user_or_message(monkeypatch):
    def explode(*a, **k):  # the model must not even be called
        raise AssertionError("LLM called")

    monkeypatch.setattr(extraction.Runner, "run", explode)
    assert await extract_and_store(None, "hello") is None
    assert await extract_and_store("demo.user@corp.com", "   ") is None


@requires_db
async def test_extract_failure_is_swallowed(monkeypatch, factless_user):
    """Extraction is a background task: an LLM failure logs and returns None, never raises."""

    async def boom(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(extraction.Runner, "run", boom)
    assert await extract_and_store(factless_user, "my laptop is a ThinkPad") is None

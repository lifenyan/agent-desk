"""Integration tests for hybrid search + knowledge tools against the live seeded DB.

Skipped wholesale when Postgres is down (`make db-up && make seed && make ingest` first).
Queries reuse eval-set phrasings so their embeddings are already in the Redis cache — a full
run makes no embedding API calls after the first `make eval`.
"""
# Implemented in M1 (knowledge tools only; user/ticket/catalog tool tests land in M2's test_tools.py).

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from sqlalchemy import text

from tests.conftest import requires_db

load_dotenv()

from app.rag.hybrid_search import hybrid_search, top_cosine  # noqa: E402
from app.tools.knowledge_tools import get_release_notes, search_knowledge_articles  # noqa: E402

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _require_embeddings(db_session):
    n = db_session.execute(
        text("SELECT count(*) FROM article_chunks WHERE embedding IS NOT NULL")
    ).scalar()
    if not n:
        pytest.skip("article_chunks not embedded yet (run `make ingest`)")


def test_returns_at_most_top_k_sorted_by_rrf(db_session):
    results = hybrid_search(db_session, "how do I reset my password", top_k=5)
    assert 0 < len(results) <= 5
    assert [r.rrf_score for r in results] == sorted((r.rrf_score for r in results), reverse=True)


def test_password_query_finds_password_article(db_session):
    results = hybrid_search(db_session, "how do I reset my password")
    assert "password" in results[0].article_title.lower()


def test_default_status_prefilter_is_published_only(db_session):
    results = hybrid_search(db_session, "how do I reset my password", top_k=50)
    assert results and all(r.status == "published" for r in results)


def test_version_prefilter(db_session):
    results = hybrid_search(db_session, "release notes", version="v5.1", doc_type="release_notes")
    assert results and all(r.version == "v5.1" for r in results)


def test_hybrid_exposes_both_branch_scores(db_session):
    results = hybrid_search(db_session, "how do I reset my password")
    assert any(r.cosine_sim is not None for r in results)
    assert any(r.lex_score is not None for r in results)


def test_refusal_signal_low_for_negative_space(db_session):
    covered = hybrid_search(db_session, "how do I reset my password")
    uncovered = hybrid_search(db_session, "how do I get a parking badge")
    assert top_cosine(covered) > top_cosine(uncovered)


def test_search_tool_payload_shape():
    payload = search_knowledge_articles("how do I reset my password")
    assert payload["sufficient_evidence"] is True
    assert payload["results"][0].keys() >= {"article_id", "article_title", "content", "rrf_score"}


def test_search_tool_rejects_invalid_category():
    payload = search_knowledge_articles("anything", category="not-a-category")
    assert payload["sufficient_evidence"] is False
    assert "invalid category" in payload["error"]


def test_get_release_notes_compare_returns_both_versions():
    payload = get_release_notes("v5.2", compare_with="v5.1")
    assert {r["version"] for r in payload["notes"]["results"]} == {"v5.2"}
    assert {r["version"] for r in payload["comparison_notes"]["results"]} == {"v5.1"}

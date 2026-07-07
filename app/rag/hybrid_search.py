"""Hybrid retrieval: pgvector cosine similarity + Postgres FTS, merged with reciprocal rank fusion.

One SQL statement (ADR-011): a vector CTE (HNSW cosine scan) and a lexical CTE (tsvector /
websearch_to_tsquery + ts_rank) are FULL OUTER JOINed and fused with RRF:

    rrf(chunk) = Σ_branches 1 / (k + rank_in_branch)        (k = 60, ADR-011)

Metadata filters (status/category/doc_type/version) are applied IDENTICALLY inside both CTEs —
on the denormalized chunk columns, so they are pre-filters during the index scans, never a
post-filter join (ADR-013). status defaults to 'published' (outdated/draft articles are
invisible unless explicitly requested). The join to knowledge_articles at the end fetches the
title for citations only — it filters nothing.

Refusal signal (ADR-017): RRF scores are rank-based — the best hit scores ~1/61 regardless of
whether it is a great match or merely the least-bad one — so they carry no absolute relevance
information. Each result therefore also exposes its raw cosine similarity, and callers gate
answering on max(cosine_sim) >= settings.retrieval_refusal_threshold.
"""
# Implemented in M1.

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cache.embedding_cache import embed_query
from app.config import get_settings

# Candidate pool per branch before fusion; >> top_k so RRF has real signal to fuse.
_POOL = 50


@dataclass(frozen=True)
class SearchResult:
    chunk_id: UUID
    article_id: UUID
    article_title: str
    chunk_index: int
    content: str
    category: str
    doc_type: str
    status: str
    version: str | None
    cosine_sim: float | None  # None if the chunk only surfaced in the lexical branch
    lex_score: float | None  # None if the chunk only surfaced in the vector branch
    rrf_score: float


_SQL = text(
    """
WITH vec AS (
    SELECT id,
           1 - (embedding <=> CAST(:qvec AS vector)) AS cosine_sim,
           ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:qvec AS vector)) AS rnk
    FROM article_chunks
    WHERE embedding IS NOT NULL
      AND status = ANY(:statuses)
      AND (CAST(:category AS text) IS NULL OR category = :category)
      AND (CAST(:doc_type AS text) IS NULL OR doc_type = :doc_type)
      AND (CAST(:version AS text) IS NULL OR version = :version)
    ORDER BY embedding <=> CAST(:qvec AS vector)
    LIMIT :pool
),
fts AS (
    SELECT id,
           ts_rank(tsv, q) AS lex_score,
           ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, q) DESC) AS rnk
    FROM article_chunks, websearch_to_tsquery('english', :query) AS q
    WHERE tsv @@ q
      AND status = ANY(:statuses)
      AND (CAST(:category AS text) IS NULL OR category = :category)
      AND (CAST(:doc_type AS text) IS NULL OR doc_type = :doc_type)
      AND (CAST(:version AS text) IS NULL OR version = :version)
    ORDER BY lex_score DESC
    LIMIT :pool
),
fused AS (
    SELECT COALESCE(vec.id, fts.id) AS id,
           vec.cosine_sim,
           fts.lex_score,
           COALESCE(1.0 / (:rrf_k + vec.rnk), 0) + COALESCE(1.0 / (:rrf_k + fts.rnk), 0)
               AS rrf_score
    FROM vec JOIN fts ON vec.id = fts.id
)
SELECT c.id AS chunk_id, c.article_id, a.title AS article_title, c.chunk_index, c.content,
       c.category, c.doc_type, c.status, c.version,
       fused.cosine_sim, fused.lex_score, fused.rrf_score
FROM fused
JOIN article_chunks c ON c.id = fused.id
JOIN knowledge_articles a ON a.id = c.article_id  -- title for citations; filters live in the CTEs
ORDER BY fused.rrf_score DESC, c.id
LIMIT :top_k
"""
)


def hybrid_search(
    session: Session,
    query: str,
    *,
    category: str | None = None,
    doc_type: str | None = None,
    version: str | None = None,
    statuses: tuple[str, ...] = ("published",),
    top_k: int | None = None,
    rrf_k: int | None = None,
) -> list[SearchResult]:
    """Run hybrid retrieval for `query` with optional metadata pre-filters."""
    settings = get_settings()
    qvec = embed_query(query)

    rows = session.execute(
        _SQL,
        {
            "qvec": "[" + ",".join(f"{x:.8f}" for x in qvec) + "]",
            "query": query,
            "statuses": list(statuses),
            "category": category,
            "doc_type": doc_type,
            "version": version,
            "pool": _POOL,
            "rrf_k": rrf_k if rrf_k is not None else settings.rrf_k,
            "top_k": top_k if top_k is not None else settings.retrieval_top_k,
        },
    ).mappings()

    return [
        SearchResult(
            chunk_id=r["chunk_id"],
            article_id=r["article_id"],
            article_title=r["article_title"],
            chunk_index=r["chunk_index"],
            content=r["content"],
            category=r["category"],
            doc_type=r["doc_type"],
            status=r["status"],
            version=r["version"],
            cosine_sim=float(r["cosine_sim"]) if r["cosine_sim"] is not None else None,
            lex_score=float(r["lex_score"]) if r["lex_score"] is not None else None,
            rrf_score=float(r["rrf_score"]),
        )
        for r in rows
    ]


def top_cosine(results: list[SearchResult]) -> float:
    """The refusal signal (ADR-017): best raw cosine similarity among the fused results."""
    return max((r.cosine_sim or 0.0 for r in results), default=0.0)

"""Ingestion pipeline: knowledge article -> chunks -> embeddings -> article_chunks rows.

This is the SINGLE write path for article_chunks (ADR-013): the article is the source of truth,
chunks are a rebuildable search projection. Each run rebuilds every article's chunks in one
transaction (delete + insert = atomic swap), re-propagating the denormalized metadata.

Idempotency comes from two layers:
- deterministic chunk ids: uuid5(namespace, f"{article_id}:{chunk_index}") — a re-run recreates
  byte-identical rows;
- the embedding cache: unchanged chunk text costs zero embedding calls on re-runs (the
  acceptance check for `make ingest` run twice).

Also embeds tickets (title + description) where embedding IS NULL — models.py records that both
embedding columns are populated in M1 with the same model (cross-table invariant 3); the
incident agent's dedup search (M2) consumes these.

M3 (ADR-024): because this is the single write path, it is also the semantic cache's
invalidation point. Change detection is a per-article CONTENT HASH — old chunk content (read
from the DB before the swap) vs freshly chunked output — NOT articles.updated_at, which is
SQLAlchemy `onupdate` (client-side only): a raw-SQL edit, exactly how a demo edits an article,
never bumps it. Zero new storage; side effect (correct): a chunker change re-hashes everything
as changed and flushes the whole semantic cache.
"""
# Implemented in M1; M3 added the semantic-cache invalidation hook.

from __future__ import annotations

import argparse
import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cache import semantic_cache
from app.cache.embedding_cache import get_or_embed
from app.db.database import SessionLocal
from app.db.models import ArticleChunk, KnowledgeArticle, Ticket
from app.rag.chunking import chunk_article

logger = logging.getLogger(__name__)

# Stable project namespace: chunk ids must not change between runs for idempotent rebuilds.
_CHUNK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "agentdesk.article_chunks")


def chunk_id(article_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    return uuid.uuid5(_CHUNK_NAMESPACE, f"{article_id}:{chunk_index}")


def content_hash(contents: Iterable[str]) -> str:
    """Order-sensitive hash of one article's chunk contents (chunk_index order)."""
    h = hashlib.sha256()
    for content in contents:
        h.update(content.encode())
        h.update(b"\x00")  # delimiter: ("ab","c") must not collide with ("a","bc")
    return h.hexdigest()


def changed_article_ids(
    old_hashes: dict[uuid.UUID, str], new_hashes: dict[uuid.UUID, str]
) -> set[str]:
    """Articles whose chunk content changed or disappeared since the last ingest (ADR-024).

    Brand-new articles (no old hash) are NOT "changed": no cached answer can cite them yet.
    """
    return {
        str(article_id)
        for article_id, old in old_hashes.items()
        if new_hashes.get(article_id) != old
    }


def _existing_content_hashes(session: Session) -> dict[uuid.UUID, str]:
    """Per-article hash of the chunk content currently in the DB (the previous ingest's output)."""
    rows = session.execute(
        select(ArticleChunk.article_id, ArticleChunk.content).order_by(
            ArticleChunk.article_id, ArticleChunk.chunk_index
        )
    ).all()
    by_article: dict[uuid.UUID, list[str]] = defaultdict(list)
    for article_id, content in rows:
        by_article[article_id].append(content)
    return {article_id: content_hash(contents) for article_id, contents in by_article.items()}


def ingest_articles(session: Session) -> dict[str, int]:
    """Rebuild all article_chunks from knowledge_articles: chunk, embed (cached), atomic swap.

    Also computes which articles CHANGED (content-hash diff, old DB chunks vs fresh output)
    and deletes the semantic-cache entries citing them (ADR-024) — this function is the single
    write path, so it is the only place staleness can originate.
    """
    articles = session.scalars(select(KnowledgeArticle).order_by(KnowledgeArticle.id)).all()
    old_hashes = _existing_content_hashes(session)  # BEFORE the swap deletes them

    chunks = [
        chunk
        for article in articles
        for chunk in chunk_article(
            article_id=article.id,
            title=article.title,
            body=article.body,
            category=article.category,
            doc_type=article.doc_type,
            status=article.status,
            version=article.version,
        )
    ]
    vectors = get_or_embed([c.content for c in chunks])

    new_contents: dict[uuid.UUID, list[str]] = defaultdict(list)
    for c in chunks:  # chunk_article yields per-article chunks in chunk_index order
        new_contents[c.article_id].append(c.content)
    new_hashes = {aid: content_hash(contents) for aid, contents in new_contents.items()}
    changed = changed_article_ids(old_hashes, new_hashes)
    invalidated = semantic_cache.invalidate_articles(changed)

    # Atomic swap inside the caller-committed transaction: readers never see a half-built index.
    session.execute(delete(ArticleChunk))
    session.add_all(
        ArticleChunk(
            id=chunk_id(c.article_id, c.chunk_index),
            article_id=c.article_id,
            chunk_index=c.chunk_index,
            content=c.content,
            category=c.category,
            doc_type=c.doc_type,
            status=c.status,
            version=c.version,
            embedding=v,
        )
        for c, v in zip(chunks, vectors)
    )
    return {
        "articles": len(articles),
        "chunks": len(chunks),
        "articles_changed": len(changed),
        "cache_entries_invalidated": invalidated,
    }


def ingest_tickets(session: Session) -> dict[str, int]:
    """Embed tickets lacking an embedding (title + description only — structured fields stay out)."""
    tickets = session.scalars(select(Ticket).where(Ticket.embedding.is_(None))).all()
    if tickets:
        vectors = get_or_embed([f"{t.title}\n\n{t.description}" for t in tickets])
        for ticket, vector in zip(tickets, vectors):
            ticket.embedding = vector
    return {"tickets_embedded": len(tickets)}


def run_ingest(*, include_tickets: bool = True) -> dict[str, int]:
    with SessionLocal() as session:
        stats = ingest_articles(session)
        if include_tickets:
            stats |= ingest_tickets(session)
        session.commit()
    return stats


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tickets", action="store_true", help="skip ticket embedding pass")
    args = parser.parse_args()

    result = run_ingest(include_tickets=not args.skip_tickets)
    print(f"ingest complete: {result}")

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
"""
# Implemented in M1.

from __future__ import annotations

import argparse
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cache.embedding_cache import get_or_embed
from app.db.database import SessionLocal
from app.db.models import ArticleChunk, KnowledgeArticle, Ticket
from app.rag.chunking import chunk_article

logger = logging.getLogger(__name__)

# Stable project namespace: chunk ids must not change between runs for idempotent rebuilds.
_CHUNK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "agentdesk.article_chunks")


def chunk_id(article_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    return uuid.uuid5(_CHUNK_NAMESPACE, f"{article_id}:{chunk_index}")


def ingest_articles(session: Session) -> dict[str, int]:
    """Rebuild all article_chunks from knowledge_articles: chunk, embed (cached), atomic swap."""
    articles = session.scalars(select(KnowledgeArticle).order_by(KnowledgeArticle.id)).all()

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
    return {"articles": len(articles), "chunks": len(chunks)}


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

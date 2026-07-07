"""Load the cached synthetic dataset in data/ into Postgres, idempotently, and print row counts.

Reads the JSON emitted by scripts/generate_data.py (never calls an LLM itself, so seeding is free and
reproducible) and inserts rows in foreign-key order. Uses session.merge() so re-running `make seed`
on an already-seeded DB is a no-op rather than a duplicate-key error. Embeddings stay NULL in M0;
M1's ingest step backfills them. The article_chunks tsvector is a generated column, so the DB fills
it automatically on insert.
"""
# Implemented in M0. Run via `make seed` (which applies migrations first).

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import func, select

from app.db.database import SessionLocal
from app.db import models

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# (model, filename) in FK-safe insertion order.
TABLES: list[tuple[type, str]] = [
    (models.User, "users.json"),
    (models.Asset, "assets.json"),
    (models.KnowledgeArticle, "knowledge_articles.json"),
    (models.ArticleChunk, "article_chunks.json"),
    (models.CatalogItem, "catalog_items.json"),
    (models.Order, "orders.json"),
    (models.Ticket, "tickets.json"),
    (models.TicketComment, "ticket_comments.json"),
    (models.UserFact, "user_facts.json"),
]


def _coerce(row: dict) -> dict:
    """Turn JSON string UUIDs into uuid.UUID for any id / *_id field."""
    out = {}
    for key, value in row.items():
        if isinstance(value, str) and (key == "id" or key.endswith("_id")):
            out[key] = uuid.UUID(value)
        else:
            out[key] = value
    return out


def _load(name: str) -> list[dict]:
    path = DATA_DIR / name
    if not path.exists():
        raise SystemExit(
            f"Missing {path.name}. Run Stage 2 generation first "
            f"(`make generate`) — this milestone stops after Stage 1 (taxonomy) for review."
        )
    return json.loads(path.read_text())


def main() -> None:
    with SessionLocal() as session:
        for model, filename in TABLES:
            for row in _load(filename):
                session.merge(model(**_coerce(row)))
            session.commit()

        print("Seeded rows per table:")
        for model, _ in TABLES:
            count = session.scalar(select(func.count()).select_from(model))
            print(f"  {model.__tablename__:<20} {count}")


if __name__ == "__main__":
    main()

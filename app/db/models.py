"""ORM models for the nine ITSM tables (users, assets, knowledge_articles, article_chunks,
catalog_items, orders, tickets, ticket_comments, user_facts).

Design notes (see design/DATA_DICTIONARY.md for per-column rationale):
- Enum-like columns are plain strings guarded by CHECK constraints; the allowed values live in the
  Python StrEnums below and are reused by data generation and tools. (Native PG ENUM types were
  rejected — see the M0 ADR — because adding a value means ALTER TYPE, whereas a CHECK is a trivial
  migration and keeps the values inline in the schema.)
- pgvector `embedding` columns (1536-dim) sit on article_chunks and tickets; both are left NULL in
  M0 and populated in M1 so the same embedding model/dim is used everywhere (cross-table invariant 3).
- `article_chunks.tsv` is a GENERATED column so the lexical half of hybrid search can never drift
  from `content`.
- The asset-belongs-to-ticket-owner invariant (cross-table invariant 4) is enforced structurally by a
  composite FK, not application code.
"""
# Implemented in M0.

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

EMBED_DIM = 1536


# --------------------------------------------------------------------------------------------------
# Enum value sets — mirrored into DB CHECK constraints, reused by data generation (M0) and tools (M2).
# --------------------------------------------------------------------------------------------------
class OS(enum.StrEnum):
    macos = "macos"
    windows = "windows"
    linux = "linux"


class Org(enum.StrEnum):
    sales = "sales"
    engineering = "engineering"
    finance = "finance"
    hr = "hr"


class UserRole(enum.StrEnum):
    employee = "employee"
    manager = "manager"
    it_agent = "it_agent"


class AssetType(enum.StrEnum):
    laptop = "laptop"
    desktop = "desktop"
    monitor = "monitor"
    phone = "phone"


class ArticleStatus(enum.StrEnum):
    published = "published"
    outdated = "outdated"
    draft = "draft"


class DocType(enum.StrEnum):
    howto = "howto"
    policy = "policy"
    release_notes = "release_notes"
    product = "product"
    onboarding = "onboarding"


class OrderStatus(enum.StrEnum):
    draft = "draft"
    submitted = "submitted"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


class ApprovalState(enum.StrEnum):
    not_required = "not_required"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class TicketType(enum.StrEnum):
    incident = "incident"
    request = "request"


class TicketCategory(enum.StrEnum):
    accounts = "accounts"
    software = "software"
    hardware = "hardware"
    network = "network"
    email = "email"
    other = "other"


class TicketPriority(enum.StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class TicketStatus(enum.StrEnum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


def _in(column: str, values: type[enum.StrEnum], *, name: str) -> CheckConstraint:
    """Build a `column IN (...)` CHECK constraint from a StrEnum's members."""
    allowed = ", ".join(f"'{v.value}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        _in("org", Org, name="ck_users_org"),
        _in("role", UserRole, name="ck_users_role"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    org: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)

    assets: Mapped[list[Asset]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        # Referenceable by the tickets composite FK that enforces "asset belongs to ticket's user".
        UniqueConstraint("id", "user_id", name="uq_assets_id_user"),
        _in("type", AssetType, name="ck_assets_type"),
        _in("os", OS, name="ck_assets_os"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    os: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)

    owner: Mapped[User] = relationship(back_populates="assets")


class KnowledgeArticle(Base):
    __tablename__ = "knowledge_articles"
    __table_args__ = (
        _in("status", ArticleStatus, name="ck_articles_status"),
        # category reuses the ticket-category enum (invariant 2) so tickets and articles line up;
        # doc_type is the finer bucket. Both back metadata-filtered retrieval (ADR-011).
        _in("category", TicketCategory, name="ck_articles_category"),
        _in("doc_type", DocType, name="ck_articles_doc_type"),
        Index("ix_articles_category", "category"),
        Index("ix_articles_doc_type", "doc_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    doc_type: Mapped[str] = mapped_column(String, nullable=False, default=DocType.howto)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default=ArticleStatus.published)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list[ArticleChunk]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class ArticleChunk(Base):
    __tablename__ = "article_chunks"
    __table_args__ = (
        UniqueConstraint("article_id", "chunk_index", name="uq_chunk_article_index"),
        # Denormalized parent metadata (copied from the article at ingest): co-locating the filter
        # with the vector lets pgvector PRE-filter during the HNSW scan instead of post-filtering
        # after a join — which under a selective filter would silently return fewer than k results.
        _in("category", TicketCategory, name="ck_chunks_category"),
        _in("doc_type", DocType, name="ck_chunks_doc_type"),
        _in("status", ArticleStatus, name="ck_chunks_status"),
        Index(
            "ix_article_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_article_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_article_chunks_category", "category"),
        Index("ix_article_chunks_doc_type", "doc_type"),
        Index("ix_article_chunks_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_articles.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # --- denormalized from the parent article (source of truth) for pre-filtered retrieval ---
    # The ingest pipeline (M1) is the single writer and re-propagates these on any article change.
    category: Mapped[str] = mapped_column(String, nullable=False)
    doc_type: Mapped[str] = mapped_column(String, nullable=False, default=DocType.howto)
    status: Mapped[str] = mapped_column(String, nullable=False, default=ArticleStatus.published)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    # Dense half of hybrid search; NULL until M1 embeds it.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    # Lexical half — GENERATED so it can never drift from `content`.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )

    article: Mapped[KnowledgeArticle] = relationship(back_populates="chunks")


class CatalogItem(Base):
    __tablename__ = "catalog_items"
    __table_args__ = (
        # Every element of os_compat must be a valid OS enum value.
        CheckConstraint(
            "os_compat IS NULL OR os_compat <@ ARRAY['macos','windows','linux']::varchar[]",
            name="ck_catalog_os_compat_valid",
        ),
        CheckConstraint("price >= 0", name="ck_catalog_price_nonneg"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Multi-valued (a set of supported OSes); text[] rather than the ERD's shorthand "string".
    os_compat: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    form_schema: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        _in("status", OrderStatus, name="ck_orders_status"),
        _in("approval_state", ApprovalState, name="ck_orders_approval_state"),
        # Cross-table invariant 5: approval_state may be 'pending' only while status = 'submitted'.
        CheckConstraint(
            "approval_state <> 'pending' OR status = 'submitted'",
            name="ck_orders_pending_requires_submitted",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("catalog_items.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=OrderStatus.draft)
    approval_state: Mapped[str] = mapped_column(
        String, nullable=False, default=ApprovalState.not_required
    )
    form_values: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        # Cross-table invariant 4: when asset_id is set, the asset must belong to the ticket's user.
        # Composite FK + MATCH SIMPLE: skipped when asset_id is NULL, enforced otherwise.
        ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["assets.id", "assets.user_id"],
            name="fk_tickets_asset_owner",
        ),
        _in("type", TicketType, name="ck_tickets_type"),
        _in("category", TicketCategory, name="ck_tickets_category"),
        _in("priority", TicketPriority, name="ck_tickets_priority"),
        _in("status", TicketStatus, name="ck_tickets_status"),
        Index(
            "ix_tickets_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_category", "category"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False, default=TicketPriority.medium)
    status: Mapped[str] = mapped_column(String, nullable=False, default=TicketStatus.open)
    # Computed from title + description only (structured fields stay out); NULL until M1 embeds it.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)

    comments: Mapped[list[TicketComment]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )


class TicketComment(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped[Ticket] = relationship(back_populates="comments")


class UserFact(Base):
    """Long-term AI memory (ADR-007) — not an ITSM entity."""

    __tablename__ = "user_facts"
    __table_args__ = (
        # Dedup key: a new fact of an existing fact_type UPDATES the old one (upsert), so
        # contradictions replace rather than accumulate.
        UniqueConstraint("user_id", "fact_type", name="uq_user_facts_user_type"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_user_facts_confidence"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    fact_type: Mapped[str] = mapped_column(String, nullable=False)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

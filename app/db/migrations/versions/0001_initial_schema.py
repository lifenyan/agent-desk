"""initial ITSM schema: pgvector extension, nine tables, indexes, and documentation comments

Revision ID: 0001
Revises:
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 1536

# Non-obvious documentation mirrored from design/DATA_DICTIONARY.md into the DB itself,
# so `\d+ <table>` in psql shows the reasoning (required by the M0 acceptance checks).
COMMENTS: list[tuple[str, str]] = [
    (
        "TABLE user_facts",
        "Long-term AI memory (ADR-007), NOT an ITSM entity: stable facts about a user, extracted at "
        "session end and injected at session start so later sessions remember earlier ones.",
    ),
    (
        "COLUMN user_facts.fact_type",
        "Category key (device_os/org/contact_preference/...). Doubles as the dedup key: a new fact of "
        "an existing type updates the old one (unique on user_id+fact_type) instead of appending.",
    ),
    ("COLUMN user_facts.fact", "The remembered content, e.g. 'owns a MacBook Pro 16'."),
    (
        "COLUMN user_facts.source",
        "Session/conversation the fact was extracted from (for debugging beliefs).",
    ),
    (
        "COLUMN user_facts.confidence",
        "0-1 extraction confidence. Merge keeps newer/higher-confidence; injection skips below a threshold.",
    ),
    ("COLUMN user_facts.updated_at", "Tiebreaker in the merge rule."),
    (
        "COLUMN catalog_items.form_schema",
        "Self-describing order form: list of {name,label,type,options,required,autofill}. `autofill` "
        "hints (asset.os, user.org) let the fulfillment agent pre-fill everything knowable — this one "
        "column is what makes that agent generic across all items instead of one form per product.",
    ),
    (
        "COLUMN orders.form_values",
        "Filled answers to the item's form_schema; keys must match the field names declared there.",
    ),
    (
        "COLUMN orders.approval_state",
        "HITL state, deliberately separate from status. 'pending' means the run ended awaiting a "
        "manager; approval places the order on a fresh run (ADR-005). Persisted here (not framework "
        "memory) so approvals survive restarts. May be 'pending' only while status='submitted'.",
    ),
    (
        "COLUMN tickets.embedding",
        "Semantic vector of title+description ONLY (structured fields stay out — they are SQL filters). "
        "Powers dedup: incident agent similarity-searches open tickets before creating a new one. "
        "NULL in M0; populated in M1.",
    ),
    (
        "COLUMN article_chunks.embedding",
        "Dense vector of content (HNSW, cosine). Catches paraphrase. NULL in M0; populated in M1.",
    ),
    (
        "COLUMN article_chunks.tsv",
        "GENERATED tsvector of the same content (GIN, ts_rank): the lexical half of hybrid search, "
        "catching exact tokens (error codes, versions). Generated so it can never drift from content.",
    ),
    (
        "COLUMN article_chunks.category",
        "Denormalized from the parent article so retrieval can PRE-filter (category/doc_type/status/"
        "version) during the vector+FTS scan rather than post-filtering after a join. The article "
        "remains the source of truth; the ingest pipeline re-propagates these on any article change.",
    ),
    (
        "COLUMN article_chunks.chunk_index",
        "0-based position within the article (unique with article_id). Reconstructs reading order and "
        "enables neighbor expansion (fetch chunks 2 and 4 when chunk 3 matches).",
    ),
    (
        "COLUMN knowledge_articles.body",
        "Full original text; chunks are DERIVED from it so articles can be re-chunked and shown in full.",
    ),
    (
        "COLUMN knowledge_articles.category",
        "Ticket-category enum value (accounts/software/hardware/network/email/other). Lets retrieval "
        "filter/boost by category and matches a ticket to same-category articles (invariant 2). Added in M0.",
    ),
    (
        "COLUMN knowledge_articles.doc_type",
        "Finer bucket within a category (howto/policy/release_notes/product/onboarding) for doc-type-"
        "filtered retrieval; release_notes is also inferable from version. Added in M0.",
    ),
    (
        "COLUMN knowledge_articles.version",
        "Product version for release-notes articles (e.g. v5.1); null for how-tos. Backs metadata-filtered "
        "'compare v5.1 vs v5.2' retrieval.",
    ),
    (
        "COLUMN assets.os",
        "macos/windows/linux — same enum as catalog_items.os_compat and article OS tags (invariant 1).",
    ),
    (
        "COLUMN catalog_items.os_compat",
        "Set of supported OS values (text[]); how the agent picks the Mac vs Windows variant.",
    ),
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("org", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.CheckConstraint("org IN ('sales','engineering','finance','hr')", name="ck_users_org"),
        sa.CheckConstraint("role IN ('employee','manager','it_agent')", name="ck_users_role"),
    )

    op.create_table(
        "assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("os", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.UniqueConstraint("id", "user_id", name="uq_assets_id_user"),
        sa.CheckConstraint("type IN ('laptop','desktop','monitor','phone')", name="ck_assets_type"),
        sa.CheckConstraint("os IN ('macos','windows','linux')", name="ck_assets_os"),
    )

    op.create_table(
        "knowledge_articles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False, server_default="howto"),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="published"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('published','outdated','draft')", name="ck_articles_status"),
        sa.CheckConstraint(
            "category IN ('accounts','software','hardware','network','email','other')",
            name="ck_articles_category",
        ),
        sa.CheckConstraint(
            "doc_type IN ('howto','policy','release_notes','product','onboarding')",
            name="ck_articles_doc_type",
        ),
    )
    op.create_index("ix_articles_category", "knowledge_articles", ["category"])
    op.create_index("ix_articles_doc_type", "knowledge_articles", ["doc_type"])

    op.create_table(
        "article_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "article_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # Denormalized parent metadata for pre-filtered retrieval (copied at ingest).
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(), nullable=False, server_default="howto"),
        sa.Column("status", sa.String(), nullable=False, server_default="published"),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=True),
        sa.Column(
            "tsv",
            TSVECTOR,
            sa.Computed("to_tsvector('english', content)", persisted=True),
        ),
        sa.UniqueConstraint("article_id", "chunk_index", name="uq_chunk_article_index"),
        sa.CheckConstraint(
            "category IN ('accounts','software','hardware','network','email','other')",
            name="ck_chunks_category",
        ),
        sa.CheckConstraint(
            "doc_type IN ('howto','policy','release_notes','product','onboarding')",
            name="ck_chunks_doc_type",
        ),
        sa.CheckConstraint("status IN ('published','outdated','draft')", name="ck_chunks_status"),
    )
    op.create_index(
        "ix_article_chunks_embedding_hnsw",
        "article_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_article_chunks_tsv", "article_chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_article_chunks_category", "article_chunks", ["category"])
    op.create_index("ix_article_chunks_doc_type", "article_chunks", ["doc_type"])
    op.create_index("ix_article_chunks_status", "article_chunks", ["status"])

    op.create_table(
        "catalog_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("os_compat", ARRAY(sa.String()), nullable=True),
        sa.Column("form_schema", JSONB, nullable=False, server_default="[]"),
        sa.CheckConstraint(
            "os_compat IS NULL OR os_compat <@ ARRAY['macos','windows','linux']::varchar[]",
            name="ck_catalog_os_compat_valid",
        ),
        sa.CheckConstraint("price >= 0", name="ck_catalog_price_nonneg"),
    )

    op.create_table(
        "orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("item_id", UUID(as_uuid=True), sa.ForeignKey("catalog_items.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("approval_state", sa.String(), nullable=False, server_default="not_required"),
        sa.Column("form_values", JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "status IN ('draft','submitted','fulfilled','cancelled')", name="ck_orders_status"
        ),
        sa.CheckConstraint(
            "approval_state IN ('not_required','pending','approved','rejected')",
            name="ck_orders_approval_state",
        ),
        sa.CheckConstraint(
            "approval_state <> 'pending' OR status = 'submitted'",
            name="ck_orders_pending_requires_submitted",
        ),
    )

    op.create_table(
        "tickets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("asset_id", UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=True),
        sa.ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["assets.id", "assets.user_id"],
            name="fk_tickets_asset_owner",
        ),
        sa.CheckConstraint("type IN ('incident','request')", name="ck_tickets_type"),
        sa.CheckConstraint(
            "category IN ('accounts','software','hardware','network','email','other')",
            name="ck_tickets_category",
        ),
        sa.CheckConstraint(
            "priority IN ('low','medium','high','critical')", name="ck_tickets_priority"
        ),
        sa.CheckConstraint(
            "status IN ('open','in_progress','resolved','closed')", name="ck_tickets_status"
        ),
    )
    op.create_index(
        "ix_tickets_embedding_hnsw",
        "tickets",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_category", "tickets", ["category"])

    op.create_table(
        "ticket_comments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "user_facts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("fact_type", sa.String(), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "fact_type", name="uq_user_facts_user_type"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_user_facts_confidence"),
    )

    for target, text in COMMENTS:
        op.execute(f"COMMENT ON {target} IS {_sql_quote(text)}")

    # Belt-and-suspenders sync: keep the denormalized chunk metadata correct if an article's filter
    # fields change without a re-chunk (especially status: published -> outdated). The transactional
    # ingest tool (M1) is the primary sync path per ADR-004; this trigger guarantees zero drift even
    # for a write that bypasses it. It fires only when a filter column is actually targeted, and only
    # rewrites chunks whose values differ.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_chunk_metadata() RETURNS trigger AS $$
        BEGIN
            UPDATE article_chunks
               SET category = NEW.category,
                   doc_type = NEW.doc_type,
                   status   = NEW.status,
                   version  = NEW.version
             WHERE article_id = NEW.id
               AND (article_chunks.category IS DISTINCT FROM NEW.category
                 OR article_chunks.doc_type IS DISTINCT FROM NEW.doc_type
                 OR article_chunks.status   IS DISTINCT FROM NEW.status
                 OR article_chunks.version  IS DISTINCT FROM NEW.version);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sync_chunk_metadata
        AFTER UPDATE OF status, category, doc_type, version ON knowledge_articles
        FOR EACH ROW EXECUTE FUNCTION sync_chunk_metadata();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sync_chunk_metadata ON knowledge_articles")
    op.execute("DROP FUNCTION IF EXISTS sync_chunk_metadata()")
    for table in (
        "user_facts",
        "ticket_comments",
        "tickets",
        "orders",
        "catalog_items",
        "article_chunks",
        "knowledge_articles",
        "assets",
        "users",
    ):
        op.drop_table(table)
    op.execute("DROP EXTENSION IF EXISTS vector")


def _sql_quote(text: str) -> str:
    """Single-quote a string literal for COMMENT ON, escaping embedded quotes."""
    return "'" + text.replace("'", "''") + "'"

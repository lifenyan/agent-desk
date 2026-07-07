"""agent session tables: Postgres-backed short-term memory (M5, ADR-030)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-07

Mirrors the schema of the Agents SDK's SQLAlchemySession (openai-agents 0.17.7,
agents/extensions/memory/sqlalchemy_session.py) EXACTLY — column for column, index for index —
so the app can instantiate it with create_tables=False and Alembic stays the single owner of
the database schema (no second, runtime CREATE TABLE path). If the pinned SDK version changes,
re-diff this migration against the SDK's table definitions.

These tables are SDK-owned storage, not ITSM entities: no ORM models in app/db/models.py on
purpose (nothing in the app queries them directly — all access goes through the SDK Session
protocol via app/memory/session_store.py).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_data", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_agent_messages_session_time", "agent_messages", ["session_id", "created_at"]
    )
    op.execute(
        "COMMENT ON TABLE agent_sessions IS "
        "'Short-term AI memory (ADR-030): SDK conversation sessions, one row per session_id. "
        "Schema mirrors openai-agents SQLAlchemySession; accessed only through the SDK Session "
        "protocol (app/memory/session_store.py), never via ORM models.'"
    )
    op.execute(
        "COMMENT ON TABLE agent_messages IS "
        "'Conversation items (user/assistant/tool messages) as SDK-serialized JSON, ordered by "
        "created_at within a session. Replaces the ADR-019 sqlite stopgap so sessions survive "
        "restarts and deploys.'"
    )


def downgrade() -> None:
    op.drop_index("idx_agent_messages_session_time", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_table("agent_sessions")

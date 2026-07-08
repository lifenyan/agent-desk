"""CMDB dependency graph: cis (configuration items) + dependencies edge table (M9, ADR-035)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-07

Infrastructure CIs (services/servers/databases) are a different kind of thing from `assets`
(user-owned end devices): a CI has dependents and an owning org, never a user owner or an OS.
One generalized node table + one edge table (rather than per-kind tables) keeps the recursive
traversal a single self-join and gives edges real FK integrity — polymorphic edges across
three tables would have neither. Teams are nodes too, so "server down -> impacted teams/users"
is one traversal, not a traversal plus a special-cased join.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

COMMENTS: list[tuple[str, str]] = [
    (
        "TABLE cis",
        "CMDB configuration items (M9, ADR-035): shared infrastructure nodes (service/server/"
        "database) plus teams-as-nodes. Deliberately separate from assets (user-owned end "
        "devices). Queried only through the graph tool (ADR-004).",
    ),
    (
        "COLUMN cis.name",
        "Stable public identity ('auth-service', 'db-server-02') — unique; tools and eval "
        "ground truth resolve CIs by name, never by UUID.",
    ),
    (
        "COLUMN cis.owner_org",
        "For team nodes: the org whose users the team represents (impact -> user resolution). "
        "For services: the owning department. NULL for servers/databases (platform-owned).",
    ),
    (
        "TABLE dependencies",
        "Directed CMDB edges: dependent DEPENDS ON dependency (service runs_on server, service "
        "uses database, service calls service, team uses service). Impact analysis traverses "
        "against the arrows; root-cause follows them. Cycle safety lives in the traversal "
        "(path-array guard), not the schema — only self-loops are structurally impossible.",
    ),
    (
        "COLUMN dependencies.dep_type",
        "Edge flavor (runs_on/uses/calls) — display metadata for tool payloads; traversal "
        "ignores it.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "cis",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ci_type", sa.String(), nullable=False),
        sa.Column("owner_org", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.UniqueConstraint("name", name="uq_cis_name"),
        sa.CheckConstraint(
            "ci_type IN ('service', 'server', 'database', 'team')", name="ck_cis_ci_type"
        ),
        sa.CheckConstraint(
            "owner_org IS NULL OR owner_org IN ('sales', 'engineering', 'finance', 'hr')",
            name="ck_cis_owner_org",
        ),
    )
    op.create_table(
        "dependencies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dependent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cis.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dependency_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cis.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dep_type", sa.String(), nullable=False),
        sa.UniqueConstraint("dependent_id", "dependency_id", name="uq_dependencies_edge"),
        sa.CheckConstraint("dependent_id <> dependency_id", name="ck_dependencies_no_self_loop"),
        sa.CheckConstraint(
            "dep_type IN ('runs_on', 'uses', 'calls')", name="ck_dependencies_dep_type"
        ),
    )
    # Traversal filters on either column depending on direction; index both.
    op.create_index("ix_dependencies_dependent", "dependencies", ["dependent_id"])
    op.create_index("ix_dependencies_dependency", "dependencies", ["dependency_id"])

    for target, comment in COMMENTS:
        op.execute("COMMENT ON {} IS '{}'".format(target, comment.replace("'", "''")))


def downgrade() -> None:
    op.drop_index("ix_dependencies_dependency", table_name="dependencies")
    op.drop_index("ix_dependencies_dependent", table_name="dependencies")
    op.drop_table("dependencies")
    op.drop_table("cis")

"""Human-readable record numbers: orders.number (ORDnnn) + tickets.number (TKTnnn) (ADR-046)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12

Users track work by quoting a short number ("what's the status of TKT042?"), not a UUID —
UUIDs stay the internal identity (FKs, tool arguments from prior payloads), numbers are the
user-facing handle. Generation is DB-side (one sequence + formatter function per table) so
uniqueness holds under concurrent inserts with no app-level coordination; three digits with
zero-padding, growing naturally past 999 (ORD999 -> ORD1000 — the formatter pads to AT LEAST
three, it never truncates, which a bare lpad(n, 3) would). Existing rows are backfilled in
id order (seeded data has no created_at; any stable order is equally meaningful) and the
sequences start after the backfill's high-water mark.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

COMMENTS: list[tuple[str, str]] = [
    (
        "COLUMN orders.number",
        "User-facing order number (ORDnnn, unique, ascending). Assigned by next_order_number() "
        "at insert; the id UUID remains the internal identity. Agents quote THIS to users.",
    ),
    (
        "COLUMN tickets.number",
        "User-facing ticket number (TKTnnn, unique, ascending). Assigned by next_ticket_number() "
        "at insert; the id UUID remains the internal identity. Agents quote THIS to users.",
    ),
]

# lpad alone truncates above the pad width ('1000' -> '100'); pad to at-least-3 instead.
_FORMATTER = """
CREATE FUNCTION {fn}() RETURNS text AS $$
    SELECT '{prefix}' || lpad(n::text, greatest(3, length(n::text)), '0')
    FROM nextval('{seq}') AS n
$$ LANGUAGE sql VOLATILE
"""


def upgrade() -> None:
    for table, prefix, seq, fn in (
        ("orders", "ORD", "order_number_seq", "next_order_number"),
        ("tickets", "TKT", "ticket_number_seq", "next_ticket_number"),
    ):
        op.execute(f"CREATE SEQUENCE {seq}")
        op.execute(_FORMATTER.format(fn=fn, prefix=prefix, seq=seq))
        op.add_column(table, sa.Column("number", sa.String(), nullable=True))
        # Backfill existing rows in stable (id) order, then point the sequence past them.
        op.execute(
            f"""
            UPDATE {table} t
            SET number = '{prefix}' || lpad(r.rn::text, greatest(3, length(r.rn::text)), '0')
            FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn FROM {table}) r
            WHERE t.id = r.id
            """
        )
        op.execute(
            f"SELECT setval('{seq}', greatest((SELECT count(*) FROM {table}), 1), "
            f"(SELECT count(*) > 0 FROM {table}))"
        )
        op.alter_column(table, "number", nullable=False, server_default=sa.text(f"{fn}()"))
        op.create_unique_constraint(f"uq_{table}_number", table, ["number"])

    for target, comment in COMMENTS:
        op.execute("COMMENT ON {} IS '{}'".format(target, comment.replace("'", "''")))


def downgrade() -> None:
    for table, seq, fn in (
        ("tickets", "ticket_number_seq", "next_ticket_number"),
        ("orders", "order_number_seq", "next_order_number"),
    ):
        op.drop_constraint(f"uq_{table}_number", table)
        op.drop_column(table, "number")
        op.execute(f"DROP FUNCTION {fn}()")
        op.execute(f"DROP SEQUENCE {seq}")

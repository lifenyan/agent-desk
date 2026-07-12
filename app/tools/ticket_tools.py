"""Ticket tools: create_ticket, update_ticket, add_ticket_comment, get_ticket_status,
search_similar_tickets.

The ONLY place ticket writes/dedup-reads touch the database (ADR-004). Plain functions first
(unit-testable against the seeded DB), then wrapped with @function_tool (`*_tool` names).
get_ticket_status started (M8) as MCP-only (ADR-040 — one tool surface, two adapters); the
record-numbers feature (ADR-046) promoted it onto the incident agent too: users quote TKTnnn
numbers and "what's the status of TKT042?" needs a direct read, which no other tool gave the
agent.

Argument trust (DESIGN NOTE in user_tools.py): the reporter's identity comes from the run
context, never an LLM argument; user-referenced ids (asset_id, ticket_id) are validated
parse -> exists -> ownership, with M0's composite FK (asset belongs to ticket's user, ADR-015)
as the DB backstop. Every miss returns a clear error dict so the SDK error-feedback loop lets
the model self-correct.

Dedup (M2): tickets are embedded at creation from "title\n\ntitle-body" EXACTLY like the M1
ingest pass (`f"{title}\n\n{description}"`, same model via the embedding cache), so new tickets
and the 300 seeded ones live in one comparable vector space. Mirroring the ADR-017 stage-1
pattern, search_similar_tickets computes the `likely_duplicate` verdict HERE (cosine vs
DEDUP_SIMILARITY_THRESHOLD), deterministically — the agent acts on the flag, it never eyeballs
raw scores.
"""
# Implemented in M2. Formal dedup eval lands in M4.

from __future__ import annotations

import re

from agents import RunContextWrapper, function_tool
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.agents.context import ChatContext
from app.cache.embedding_cache import get_or_embed
from app.db.database import SessionLocal
from app.db.models import (
    Asset,
    Ticket,
    TicketCategory,
    TicketComment,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from app.tools.user_tools import enum_error, parse_uuid_arg, resolve_acting_user

# Tunable dedup gate, spot-checked against the 300 seeded tickets (ignore/m2_dedup_sweep.py,
# ADR-021). Measured at 0.80: same-issue pairs (shared title, reworded description) keep 95%
# (p5=0.798), cross-issue nearest neighbors flag 0% (max=0.797). But a FRESH report of an old
# issue (formal re-draft, not shared phrasing) scores only 0.59-0.77 against its true group —
# overlapping the cross-issue NN range (median 0.667, p95 0.754) — so NO single threshold
# separates "same issue, new report" from "similar but different issue". The flag therefore
# means "near-certain duplicate, link without asking"; the 0.60-0.80 band is decided by the
# incident agent reading the candidates (the same stage-2 judgment ADR-017 forced on refusal).
DEDUP_SIMILARITY_THRESHOLD = 0.80


def _embed_ticket_text(title: str, description: str) -> list[float]:
    # MUST match app/rag/ingest.py::ingest_tickets — one vector space for old + new tickets.
    return get_or_embed([f"{title}\n\n{description}"])[0]


_TICKET_NUMBER_RE = re.compile(r"^TKT\d{3,}$")


def _resolve_ticket_ref(session: Session, ticket_ref: str) -> Ticket | dict:
    """Fetch a ticket by UUID or by user-facing number (TKTnnn, ADR-046).

    Users quote numbers ("what's the status of TKT042?"), prior tool payloads carry UUIDs —
    the ticket_id argument accepts both. Error dicts follow the house feedback pattern."""
    ref = (ticket_ref or "").strip().upper()
    if _TICKET_NUMBER_RE.match(ref):
        ticket = session.scalar(select(Ticket).where(Ticket.number == ref))
    else:
        ticket_uuid = parse_uuid_arg(ticket_ref, "ticket_id")
        if isinstance(ticket_uuid, dict):
            return ticket_uuid
        ticket = session.get(Ticket, ticket_uuid)
    if ticket is None:
        return {"error": f"ticket {ticket_ref} not found"}
    return ticket


def create_ticket(
    ctx: RunContextWrapper[ChatContext],
    title: str,
    description: str,
    category: TicketCategory,
    priority: TicketPriority = TicketPriority.medium,
    asset_id: str | None = None,
    type: TicketType = TicketType.incident,
) -> dict:
    """Create a support ticket for the current user. Search for duplicates FIRST
    (search_similar_tickets) — never create a ticket you have not dedup-checked.

    Args:
        title: Short summary of the issue, e.g. "VPN drops every 30 minutes".
        description: Full description: what happens, what the user tried, error messages.
        category: One of the ticket categories (accounts, software, hardware, network, email, other).
        priority: Business impact (low, medium, high, critical). Default medium.
        asset_id: Optional UUID of the affected asset — ONLY an id returned by get_user_assets.
        type: "incident" (something is broken, default) or "request".
    """
    for value, enum_cls, what in (
        (category, TicketCategory, "category"),
        (priority, TicketPriority, "priority"),
        (type, TicketType, "type"),
    ):
        if error := enum_error(value, enum_cls, what):
            return error
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        asset_uuid = None
        if asset_id is not None:
            asset_uuid = parse_uuid_arg(asset_id, "asset_id")
            if isinstance(asset_uuid, dict):
                return asset_uuid
            asset = session.get(Asset, asset_uuid)
            if asset is None:
                return {"error": f"asset {asset_id} not found — use an id from get_user_assets"}
            if asset.user_id != user.id:  # composite FK (ADR-015) would also reject this
                return {"error": f"asset {asset_id} does not belong to the current user"}
        ticket = Ticket(
            user_id=user.id,
            asset_id=asset_uuid,
            type=type,
            title=title,
            description=description,
            category=category,
            priority=priority,
            status=TicketStatus.open,
            embedding=_embed_ticket_text(title, description),  # embedded AT creation (invariant 3)
        )
        session.add(ticket)
        session.commit()
        return {
            "ticket": {
                "ticket_id": str(ticket.id),
                "number": ticket.number,  # the user-facing handle — quote THIS, never the id
                "title": ticket.title,
                "category": ticket.category,
                "priority": ticket.priority,
                "status": ticket.status,
            }
        }


def update_ticket(
    ctx: RunContextWrapper[ChatContext],
    ticket_id: str,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    category: TicketCategory | None = None,
) -> dict:
    """Update the status, priority, or category of one of the current user's OWN tickets.

    Only structured fields are updatable (title/description are immutable — they feed the
    ticket's dedup embedding). Pass only the fields to change.

    Args:
        ticket_id: UUID or ticket number (e.g. "TKT042") of the ticket to update.
        status: New status (open, in_progress, resolved, closed).
        priority: New priority (low, medium, high, critical).
        category: New category (accounts, software, hardware, network, email, other).
    """
    for value, enum_cls, what in (
        (status, TicketStatus, "status"),
        (priority, TicketPriority, "priority"),
        (category, TicketCategory, "category"),
    ):
        if error := enum_error(value, enum_cls, what):
            return error
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        ticket = _resolve_ticket_ref(session, ticket_id)
        if isinstance(ticket, dict):
            return ticket
        if ticket.user_id != user.id:
            return {"error": f"ticket {ticket_id} does not belong to the current user"}
        if status is None and priority is None and category is None:
            return {"error": "nothing to update: pass status, priority, and/or category"}
        if status is not None:
            ticket.status = status
        if priority is not None:
            ticket.priority = priority
        if category is not None:
            ticket.category = category
        session.commit()
        return {
            "ticket": {
                "ticket_id": str(ticket.id),
                "number": ticket.number,
                "status": ticket.status,
                "priority": ticket.priority,
                "category": ticket.category,
            }
        }


def add_ticket_comment(ctx: RunContextWrapper[ChatContext], ticket_id: str, body: str) -> dict:
    """Add a comment to an existing ticket, authored by the current user.

    Used mainly for dedup-linking: when the user's issue duplicates an existing ticket
    (possibly ANOTHER user's — a widespread outage), comment there ("Also reported by …")
    instead of opening a new ticket. Commenting deliberately requires existence, not
    ownership: "me too" reports on someone else's ticket are the point.

    Args:
        ticket_id: UUID or ticket number (e.g. "TKT042") of the ticket to comment on.
        body: The comment text.
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        ticket = _resolve_ticket_ref(session, ticket_id)
        if isinstance(ticket, dict):
            return ticket
        comment = TicketComment(ticket_id=ticket.id, author_id=user.id, body=body)
        session.add(comment)
        session.commit()
        return {
            "comment": {
                "comment_id": str(comment.id),
                "ticket_id": str(ticket.id),
                "ticket_number": ticket.number,
                "ticket_title": ticket.title,
                "ticket_status": ticket.status,
            }
        }


def get_ticket_status(ctx: RunContextWrapper[ChatContext], ticket_id: str) -> dict:
    """Get the current status of one of the current user's OWN tickets: status, priority,
    category, and the latest comment (support updates land there).

    Args:
        ticket_id: UUID or ticket number (e.g. "TKT042") of the ticket to look up.
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        ticket = _resolve_ticket_ref(session, ticket_id)
        if isinstance(ticket, dict):
            return ticket
        # Ownership: reading another user's ticket is an information leak (MCP exposes this
        # to external clients — M8). Contrast add_ticket_comment, which deliberately allows
        # foreign tickets because "me too" dedup-linking is its purpose.
        if ticket.user_id != user.id:
            return {"error": f"ticket {ticket_id} does not belong to the current user"}
        latest = max(ticket.comments, key=lambda c: c.created_at, default=None)
        return {
            "ticket": {
                "ticket_id": str(ticket.id),
                "number": ticket.number,
                "title": ticket.title,
                "status": ticket.status,
                "priority": ticket.priority,
                "category": ticket.category,
                "type": ticket.type,
                "comment_count": len(ticket.comments),
                "latest_comment": latest.body if latest else None,
            }
        }


def search_similar_tickets(
    ctx: RunContextWrapper[ChatContext],
    issue_text: str,
    top_k: int = 5,
    include_closed: bool = False,
) -> dict:
    """Search existing tickets for near-duplicates of an issue (pgvector cosine similarity).

    ALWAYS call this before create_ticket. Candidates flagged `likely_duplicate: true` should
    be linked (add_ticket_comment on the existing ticket) instead of creating a new ticket;
    otherwise create a new one. The search spans ALL users' tickets — a colleague may have
    already reported the same outage.

    Args:
        issue_text: The issue being reported — pass "title\\n\\ndescription" of your draft.
        top_k: Max candidates to return (default 5).
        include_closed: Also match closed tickets (default false — closed issues are resolved,
            a recurrence deserves a fresh ticket).
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        qvec = get_or_embed([issue_text])[0]
        rows = session.execute(
            sql_text(
                """
                SELECT id, number, user_id, title, status, category, priority,
                       1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM tickets
                WHERE embedding IS NOT NULL
                  AND (:include_closed OR status <> 'closed')
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :top_k
                """
            ),
            {
                "qvec": "[" + ",".join(f"{x:.8f}" for x in qvec) + "]",
                "include_closed": include_closed,
                "top_k": top_k,
            },
        ).mappings()
        return {
            "dedup_threshold": DEDUP_SIMILARITY_THRESHOLD,
            "guidance": (
                "likely_duplicate=true means near-certain duplicate: link, don't create. "
                "The flag NOT being set does NOT mean 'not a duplicate' — embeddings can't "
                "separate a fresh report of the same issue from a similar-but-different one. "
                "Read the candidates: an OPEN ticket describing the same failure of the same "
                "thing IS the same issue."
            ),
            "candidates": [
                {
                    "ticket_id": str(r["id"]),
                    "number": r["number"],
                    "title": r["title"],
                    "status": r["status"],
                    "category": r["category"],
                    "priority": r["priority"],
                    "similarity": round(float(r["similarity"]), 4),
                    "likely_duplicate": float(r["similarity"]) >= DEDUP_SIMILARITY_THRESHOLD,
                    "belongs_to_current_user": r["user_id"] == user.id,
                }
                for r in rows
            ],
        }


def get_ticket_details(ref: str) -> dict | None:
    """Full ticket detail for the chat UI's ?ticket= page (routes_records) — NOT an agent tool.

    Accepts TKTnnn or UUID. Auth is deliberately out of scope (the stated cut line, same as
    routes_approvals): whoever reaches the API sees the demo data. None = not found."""
    with SessionLocal() as session:
        ticket = _resolve_ticket_ref(session, ref)
        if isinstance(ticket, dict):
            return None
        comments = sorted(ticket.comments, key=lambda c: c.created_at)
        return {
            "number": ticket.number,
            "title": ticket.title,
            "description": ticket.description,
            "type": ticket.type,
            "category": ticket.category,
            "priority": ticket.priority,
            "status": ticket.status,
            "comments": [
                {"body": c.body, "created_at": c.created_at.isoformat()} for c in comments
            ],
        }


# --- Agents SDK wrappers (schema derived from the signatures + docstrings above) ---
create_ticket_tool = function_tool(create_ticket)
get_ticket_status_tool = function_tool(get_ticket_status)
update_ticket_tool = function_tool(update_ticket)
add_ticket_comment_tool = function_tool(add_ticket_comment)
search_similar_tickets_tool = function_tool(search_similar_tickets)

"""User tools: get_user_profile, get_user_assets — read the requesting user's profile and owned assets.

This module also owns the shared argument-trust helpers (`resolve_acting_user`, `parse_uuid_arg`)
that ticket_tools and catalog_tools import — identity is defined once, here.
"""
# Implemented in M2. M3 wraps lookups with response_cache.
#
# DESIGN NOTE (M2) — argument trust: schema/enum constraints (see knowledge_tools.py) enforce the
# SHAPE of a tool arg but never its SEMANTICS. "Alice" and a user UUID are both valid strings, so
# nothing stops the LLM from passing a name where an id belongs, a hallucinated id, or another
# user's id. Guard this in layers, strongest first:
#   1. DON'T let the LLM supply identity. The requesting user's id comes from the trusted session,
#      not a model argument: read it from the run context — `ctx.context.user_id` (ChatContext in
#      app/agents/context.py) — via a `RunContextWrapper[ChatContext]` tool param. So these tools
#      take NO user_id argument. This is correctness AND a security boundary (a prompt-injected
#      model can't act as someone else if it can't name them).
#   2. For entities the USER genuinely references (create_ticket asset_id, place order item_id,
#      "link to ticket #4821"): validate referentially IN the tool (ADR-004 = only DB path) —
#      (a) parse/format (uuid.UUID(x) rejects names/junk), (b) existence (session.get -> 404s a
#      well-formed hallucination), (c) ownership (belongs to ctx user; M0's composite-FK invariant
#      ADR-015 is the DB backstop). Return a clear error dict on any miss so the SDK feeds it back
#      and the model self-corrects.
#   3. Resolve name->id with an explicit lookup tool, never an LLM guess; return candidates when
#      ambiguous instead of auto-picking.
# Assert the identity/ownership behavior in the M4 e2e eval (ADR-010): a ticket/order must be
# created for the RIGHT user against a scratch DB.
#
# Payload keys: routes_chat._collect_citations treats any tool payload carrying a "results" key as
# knowledge citations — action-tool payloads must NEVER use a bare "results" key (use "assets",
# "items", "candidates", ...), or tickets/orders leak into the citations panel.

from __future__ import annotations

import uuid

from agents import RunContextWrapper, function_tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.context import ChatContext
from app.db.database import SessionLocal
from app.db.models import Asset, User

# ---------------------------------------------------------------------------------------------
# Shared argument-trust helpers (layer 1 + layer 2a of the DESIGN NOTE above)
# ---------------------------------------------------------------------------------------------


def resolve_acting_user(session: Session, ctx: RunContextWrapper[ChatContext]) -> User | dict:
    """Resolve the ACTING user from the trusted run context — never from an LLM argument.

    `ChatContext.user_id` is whatever the API trusted at request time (a user UUID or the
    login email). Returns the ORM User, or a clear error dict (for the SDK error-feedback
    loop) when the context carries no user or an unknown one.
    """
    raw = ctx.context.user_id if ctx.context is not None else None
    if not raw:
        return {
            "error": "no acting user: this session has no user_id. Ask the user to sign in "
            "(the Streamlit sidebar user picker) — do not guess or ask them to type an id."
        }
    try:
        user = session.get(User, uuid.UUID(raw))
    except ValueError:
        user = session.scalar(select(User).where(User.email == raw))
    if user is None:
        return {"error": f"acting user {raw!r} not found"}
    return user


def parse_uuid_arg(value: str, what: str) -> uuid.UUID | dict:
    """Layer-2a format guard for USER-REFERENCED ids: reject names/junk before touching the DB."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return {"error": f"invalid {what} {value!r}: expected a UUID (never pass names here)"}


def enum_error(value: object, enum_cls: type, what: str) -> dict | None:
    """Belt-and-suspenders enum guard for DIRECT/programmatic callers (knowledge_tools precedent):
    the agent path is schema-constrained, but a direct caller passing junk must get an error
    dict, not an IntegrityError from the DB CHECK."""
    valid = {m.value for m in enum_cls}
    if value is None or value in valid:
        return None
    return {"error": f"invalid {what} {value!r}; valid: {sorted(valid)}"}


# ---------------------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------------------


def get_user_profile(ctx: RunContextWrapper[ChatContext]) -> dict:
    """Get the current user's profile: name, email, org (cost center), and role.

    Use this to fill order forms (e.g. cost_center comes from the user's org) and to set a
    ticket's org — never guess these values.
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        return {
            "user": {
                "id": str(user.id),
                "name": user.name,
                "email": user.email,
                "org": user.org,
                "role": user.role,
            }
        }


def get_user_assets(ctx: RunContextWrapper[ChatContext]) -> dict:
    """List the hardware assets (laptop, desktop, monitor, phone) owned by the current user.

    Use this to resolve the user's OS before filtering the catalog, and to attach the right
    asset_id to a hardware ticket. Assets belong to the signed-in user only.
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        assets = session.scalars(select(Asset).where(Asset.user_id == user.id)).all()
        return {
            "assets": [
                {"asset_id": str(a.id), "type": a.type, "os": a.os, "model": a.model}
                for a in assets
            ]
        }


# --- Agents SDK wrappers (schema derived from the signatures + docstrings above) ---
get_user_profile_tool = function_tool(get_user_profile)
get_user_assets_tool = function_tool(get_user_assets)

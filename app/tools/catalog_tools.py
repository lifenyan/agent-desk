"""Catalog tools: list_catalog_items, place_catalog_order, request_approval — plus the
human-only approval path (list_pending_orders / approve_order / reject_order) used by
routes_approvals. ADR-004: this module is the ONLY DB access path for catalog_items/orders,
so the approvals API goes through it too — but approve/reject are deliberately NOT wrapped
as agent tools: approval authority is human-only (ADR-005), and an LLM that can approve its
own orders has no HITL at all.

Order state machine (respects M0's CHECK — approval_state='pending' only while status='submitted'):

    place_catalog_order, price <= threshold:  -> status=submitted, approval_state=not_required (placed)
    place_catalog_order, price >  threshold:  -> status=draft,     approval_state=not_required
    request_approval(draft order)             -> status=submitted, approval_state=pending  (run ends)
    approve_order (human, fresh process)      -> approval_state=approved                   (placed)
    reject_order  (human, fresh process)      -> status=cancelled, approval_state=rejected

The price is ALWAYS read from the catalog row, never from an LLM argument — a model cannot
talk an order under the approval threshold (DESIGN NOTE in user_tools.py).
"""
# Implemented in M2 (HITL mechanism choice recorded in ADR-020). M3 wrapped list_catalog_items
# with the response cache (ADR-025) — the plain function is decorated BEFORE function_tool.

from __future__ import annotations

import re
import uuid

from agents import RunContextWrapper, function_tool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.context import ChatContext
from app.cache.response_cache import cache_response
from app.config import get_settings
from app.db.database import SessionLocal
from app.db.models import OS, ApprovalState, CatalogItem, Order, OrderStatus, User
from app.tools.user_tools import enum_error, parse_uuid_arg, resolve_acting_user


class FormValue(BaseModel):
    """One filled form field (name/value pairs — strict tool schemas can't take open dicts)."""

    name: str
    value: str


def _requires_approval(item: CatalogItem) -> bool:
    return float(item.price) > get_settings().hitl_approval_threshold_usd


def _validate_form_values(item: CatalogItem, form_values: list[FormValue]) -> dict | None:
    """Check filled values against the item's form_schema; error dict on any miss."""
    schema = {field["name"]: field for field in item.form_schema}
    filled = {fv.name: fv.value for fv in form_values}
    unknown = sorted(filled.keys() - schema.keys())
    if unknown:
        return {"error": f"unknown form fields {unknown}; this item's fields: {sorted(schema)}"}
    missing = sorted(
        name for name, field in schema.items() if field.get("required") and name not in filled
    )
    if missing:
        return {"error": f"missing required form fields {missing} — ask the user, don't invent"}
    for name, value in filled.items():
        options = schema[name].get("options")
        if options and value not in options:
            return {"error": f"form field {name!r}: {value!r} is not one of {options}"}
    return None


def _order_summary(order: Order) -> str:
    """One human-readable line for the order's CURRENT position in the ADR-020 state machine."""
    if order.status == OrderStatus.draft:
        return "draft — not yet submitted"
    if order.status == OrderStatus.cancelled:
        if order.approval_state == ApprovalState.rejected:
            return "rejected by the manager and cancelled"
        return "cancelled"
    if order.status == OrderStatus.fulfilled:
        return "fulfilled — delivered/provisioned"
    if order.approval_state == ApprovalState.pending:
        return "awaiting manager approval"
    if order.approval_state == ApprovalState.approved:
        return "approved by the manager — order placed"
    return "placed (no approval needed)"


_ORDER_NUMBER_RE = re.compile(r"^ORD\d{3,}$")


def _resolve_order_ref(session: Session, order_ref: str) -> Order | dict:
    """Fetch an order by UUID or by user-facing number (ORDnnn, ADR-046) — mirrors
    ticket_tools._resolve_ticket_ref."""
    ref = (order_ref or "").strip().upper()
    if _ORDER_NUMBER_RE.match(ref):
        order = session.scalar(select(Order).where(Order.number == ref))
    else:
        order_uuid = parse_uuid_arg(order_ref, "order_id")
        if isinstance(order_uuid, dict):
            return order_uuid
        order = session.get(Order, order_uuid)
    if order is None:
        return {"error": f"order {order_ref} not found"}
    return order


def _order_payload(order: Order, item: CatalogItem) -> dict:
    return {
        "order": {
            "order_id": str(order.id),
            "number": order.number,  # the user-facing handle — quote THIS, never the id
            "item": item.name,
            "price_usd": float(item.price),  # from the catalog row, never the model
            "status": order.status,
            "approval_state": order.approval_state,
            "form_values": order.form_values,
        }
    }


# ---------------------------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------------------------


# Response-cache key = the one argument (ADR-025). getattr covers both call shapes: the agent
# path passes the OS StrEnum, direct callers may pass a raw string; an invalid string produces
# an enum_error dict, which is never stored.
@cache_response(key_fn=lambda os_filter=None: f"os={getattr(os_filter, 'value', os_filter)}")
def list_catalog_items(os_filter: OS | None = None) -> dict:
    """List orderable catalog items (hardware, software licenses, services) with price and
    order-form schema.

    Args:
        os_filter: Only return items compatible with this OS (macos, windows, linux). Resolve
            the user's OS from get_user_assets first — don't ask if an asset already tells you.
    """
    if error := enum_error(os_filter, OS, "os_filter"):
        return error
    with SessionLocal() as session:
        items = session.scalars(select(CatalogItem).order_by(CatalogItem.name)).all()
        if os_filter is not None:
            items = [i for i in items if i.os_compat is None or os_filter in i.os_compat]
        threshold = get_settings().hitl_approval_threshold_usd
        return {
            "approval_threshold_usd": threshold,
            "items": [
                {
                    "item_id": str(i.id),
                    "name": i.name,
                    "price_usd": float(i.price),
                    "os_compat": i.os_compat,  # None = OS-independent
                    "requires_approval": _requires_approval(i),
                    "form_schema": i.form_schema,
                }
                for i in items
            ],
        }


def get_my_orders(ctx: RunContextWrapper[ChatContext]) -> dict:
    """List ALL of the current user's catalog orders with their CURRENT status and approval
    state. Call this whenever the user asks about an order that already exists ("what's the
    status of my order?", "was it approved yet?") — approvals happen OUTSIDE this chat in a
    manager view, so the conversation history is always stale; only this tool is current.
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        orders = session.scalars(select(Order).where(Order.user_id == user.id)).all()
        entries = []
        for order in orders:
            item = session.get(CatalogItem, order.item_id)
            entries.append(
                {
                    "order_id": str(order.id),
                    "number": order.number,
                    "item": item.name,
                    "price_usd": float(item.price),
                    "status": order.status,
                    "approval_state": order.approval_state,
                    "summary": _order_summary(order),
                }
            )
        return {
            "orders": entries,
            "guidance": (
                "Report from `summary` — it already encodes the status/approval_state "
                "combination. Identify orders to the user by their number (ORDnnn) plus item "
                "and price; the id UUID is internal, never show it."
            ),
        }


def place_catalog_order(
    ctx: RunContextWrapper[ChatContext], item_id: str, form_values: list[FormValue]
) -> dict:
    """Place a catalog order for the current user. Confirm the item and filled form with the
    user BEFORE calling this.

    Orders at or under the approval threshold are placed immediately. Orders above it are
    saved as a DRAFT: call request_approval with the returned order_id to submit it for
    manager approval.

    Args:
        item_id: UUID of the catalog item — ONLY an id returned by list_catalog_items.
        form_values: The item's form fields filled in (pre-fill from get_user_profile /
            get_user_assets where the form's autofill hints say so; ask the user for the rest).
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        item_uuid = parse_uuid_arg(item_id, "item_id")
        if isinstance(item_uuid, dict):
            return item_uuid
        item = session.get(CatalogItem, item_uuid)
        if item is None:
            return {"error": f"catalog item {item_id} not found — use list_catalog_items ids"}
        if error := _validate_form_values(item, form_values):
            return error
        needs_approval = _requires_approval(item)  # price from the DB row (never trust the LLM)
        order = Order(
            user_id=user.id,
            item_id=item.id,
            status=OrderStatus.draft if needs_approval else OrderStatus.submitted,
            approval_state=ApprovalState.not_required,
            form_values={fv.name: fv.value for fv in form_values},
        )
        session.add(order)
        session.commit()
        payload = _order_payload(order, item)
        if needs_approval:
            payload["next_step"] = (
                f"This item costs ${float(item.price):.2f}, above the "
                f"${get_settings().hitl_approval_threshold_usd:.0f} approval threshold. The order "
                "is saved as a draft — call request_approval with this order_id to submit it "
                "for manager approval."
            )
        else:
            payload["next_step"] = "Order placed — no approval needed."
        return payload


def request_approval(ctx: RunContextWrapper[ChatContext], order_id: str) -> dict:
    """Submit one of the current user's draft orders for manager approval.

    After this the run is over for you: tell the user the order is awaiting approval and STOP.
    A human manager approves or rejects it later in the approvals view — never claim the order
    was placed, and never try to approve it yourself.

    Args:
        order_id: UUID or order number (e.g. "ORD019") of the draft order returned by
            place_catalog_order.
    """
    with SessionLocal() as session:
        user = resolve_acting_user(session, ctx)
        if isinstance(user, dict):
            return user
        order = _resolve_order_ref(session, order_id)
        if isinstance(order, dict):
            return order
        if order.user_id != user.id:
            return {"error": f"order {order_id} does not belong to the current user"}
        if order.approval_state == ApprovalState.pending:
            item = session.get(CatalogItem, order.item_id)
            return _order_payload(order, item) | {"note": "already awaiting approval"}
        if order.status != OrderStatus.draft:
            return {
                "error": f"order {order_id} is {order.status!r}, not a draft awaiting submission"
            }
        item = session.get(CatalogItem, order.item_id)
        if not _requires_approval(item):
            # Self-correcting path: no approval actually needed — just place it.
            order.status = OrderStatus.submitted
            session.commit()
            return _order_payload(order, item) | {
                "note": "approval not required at this price; order placed directly"
            }
        # Draft -> submitted+pending in one transition (the CHECK allows pending only on submitted).
        order.status = OrderStatus.submitted
        order.approval_state = ApprovalState.pending
        session.commit()
        return _order_payload(order, item) | {
            "note": "order submitted for manager approval; the run should end after informing the user"
        }


# ---------------------------------------------------------------------------------------------
# Human-only approval path (routes_approvals) — NOT agent tools (ADR-005)
# ---------------------------------------------------------------------------------------------


def _serialize_pending(session: Session, order: Order) -> dict:
    item = session.get(CatalogItem, order.item_id)
    requester = session.get(User, order.user_id)
    return {
        "order_id": str(order.id),
        "number": order.number,
        "item": item.name,
        "price_usd": float(item.price),
        "requester": requester.email,
        "requester_name": requester.name,
        "org": requester.org,
        "status": order.status,
        "approval_state": order.approval_state,
        "form_values": order.form_values,
    }


def list_pending_orders() -> list[dict]:
    """All orders awaiting approval, for the manager view."""
    with SessionLocal() as session:
        orders = session.scalars(
            select(Order).where(Order.approval_state == ApprovalState.pending)
        ).all()
        return [_serialize_pending(session, o) for o in orders]


def approve_order(order_id: str) -> dict:
    """Approve a pending order — completes its placement. Runs in a FRESH process/run: nothing
    from the original agent run is needed (the pending row IS the persisted approval request).

    Caller is trusted code (routes_approvals validates the UUID at the route boundary), so a
    malformed id raises rather than returning an LLM-feedback error dict."""
    with SessionLocal() as session:
        order = session.get(Order, uuid.UUID(order_id))
        if order is None:
            return {"error": f"order {order_id} not found"}
        if order.approval_state != ApprovalState.pending:
            return {"error": f"order {order_id} is {order.approval_state!r}, not pending"}
        order.approval_state = ApprovalState.approved  # status stays 'submitted' => placed
        session.commit()
        return _serialize_pending(session, order)


def reject_order(order_id: str) -> dict:
    """Reject a pending order: cancels it (CHECK: 'pending' may not outlive 'submitted')."""
    with SessionLocal() as session:
        order = session.get(Order, uuid.UUID(order_id))
        if order is None:
            return {"error": f"order {order_id} not found"}
        if order.approval_state != ApprovalState.pending:
            return {"error": f"order {order_id} is {order.approval_state!r}, not pending"}
        order.approval_state = ApprovalState.rejected
        order.status = OrderStatus.cancelled
        session.commit()
        return _serialize_pending(session, order)


# --- Agents SDK wrappers (schema derived from the signatures + docstrings above) ---
list_catalog_items_tool = function_tool(list_catalog_items)
get_my_orders_tool = function_tool(get_my_orders)
place_catalog_order_tool = function_tool(place_catalog_order)
request_approval_tool = function_tool(request_approval)

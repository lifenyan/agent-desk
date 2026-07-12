"""Unit tests for the M2 action tools (user/ticket/catalog) against the live seeded DB.

Skipped wholesale when Postgres is down (test_retrieval.py precedent). These call the PLAIN
functions (the SDK wrappers share the same bodies); the run context is faked with a real
RunContextWrapper + ChatContext, exactly what the Runner passes.

Covered guards (DESIGN NOTE in user_tools.py):
- identity: no/unknown user in context -> error dict; identity is never a tool argument;
- ownership: acting on another user's asset/ticket/order -> error dict;
- format: invalid uuid/enum from a DIRECT caller -> error dict, never a crash;
- money: >$500 items can only reach approval_state='pending', never direct placement.

Ticket-creation tests embed once via the Redis cache (fixed titles => later runs are free).
A cleanup fixture deletes any orders/tickets created by a test, so the seeded DB (incl. the
two demo pending orders the Streamlit walkthrough relies on) stays pristine.
"""
# Implemented in M2.

from __future__ import annotations

import re
import uuid

import pytest
from dotenv import load_dotenv
from sqlalchemy import select

from tests.conftest import requires_db

load_dotenv()

from agents import RunContextWrapper  # noqa: E402

from app.agents.context import ChatContext  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.db.models import (  # noqa: E402
    ApprovalState,
    Asset,
    CatalogItem,
    Order,
    OrderStatus,
    Ticket,
    TicketComment,
    TicketStatus,
    User,
)
from app.tools.catalog_tools import (  # noqa: E402
    FormValue,
    _order_summary,
    approve_order,
    get_my_orders,
    get_order_details,
    list_catalog_items,
    list_pending_orders,
    place_catalog_order,
    reject_order,
    request_approval,
)
from app.tools.slack_tools import post_slack_message  # noqa: E402
from app.tools.ticket_tools import (  # noqa: E402
    add_ticket_comment,
    create_ticket,
    get_ticket_details,
    get_ticket_status,
    search_similar_tickets,
    update_ticket,
)
from app.tools.user_tools import get_user_assets, get_user_profile  # noqa: E402

pytestmark = requires_db

DEMO_EMAIL = "demo.user@corp.com"


def ctx_for(user_id: str | None) -> RunContextWrapper[ChatContext]:
    return RunContextWrapper(context=ChatContext(user_id=user_id))


@pytest.fixture
def demo_ctx() -> RunContextWrapper[ChatContext]:
    return ctx_for(DEMO_EMAIL)


@pytest.fixture
def demo_user_id():
    with SessionLocal() as s:
        return s.scalar(select(User.id).where(User.email == DEMO_EMAIL))


@pytest.fixture
def other_user_asset(demo_user_id):
    """Some asset NOT owned by the demo user, for ownership-guard tests."""
    with SessionLocal() as s:
        return s.scalar(select(Asset.id).where(Asset.user_id != demo_user_id).limit(1))


@pytest.fixture
def clean_writes():
    """Delete orders/tickets/comments created during the test, keeping the seeded DB
    byte-identical — the Streamlit demo depends on the seeded pending orders.

    Comments are diffed EXPLICITLY, not left to the ticket cascade: the dedup-comment test
    writes onto a SEEDED foreign ticket, so its comment survives the cascade — found in M5
    as the source of the "license expired" comment accumulating once per pytest run (the
    leak M4 blamed on killed-process windows; that diagnosis was wrong)."""
    with SessionLocal() as s:
        before_orders = set(s.scalars(select(Order.id)))
        before_tickets = set(s.scalars(select(Ticket.id)))
        before_comments = set(s.scalars(select(TicketComment.id)))
    yield
    with SessionLocal() as s:
        for comment in s.scalars(
            select(TicketComment).where(TicketComment.id.notin_(before_comments))
        ):
            s.delete(comment)
        for order in s.scalars(select(Order).where(Order.id.notin_(before_orders))):
            s.delete(order)
        for ticket in s.scalars(select(Ticket).where(Ticket.id.notin_(before_tickets))):
            s.delete(ticket)  # ORM cascade removes any remaining comments
        s.commit()


def _form_values_for(item: dict) -> list[FormValue]:
    """Fill an item's required form fields with schema-valid values."""
    return [
        FormValue(name=f["name"], value=(f.get("options") or ["test"])[0])
        for f in item["form_schema"]
        if f.get("required")
    ]


def _cheap_and_pricey():
    """(item <= threshold, item > threshold) from the live catalog payload."""
    items = list_catalog_items()["items"]
    threshold = get_settings().hitl_approval_threshold_usd
    cheap = min((i for i in items if i["price_usd"] <= threshold), key=lambda i: i["price_usd"])
    pricey = min((i for i in items if i["price_usd"] > threshold), key=lambda i: i["price_usd"])
    return cheap, pricey


# ---------------------------------------------------------------------------------------------
# Identity (layer 1): the acting user comes from context, or the tool refuses
# ---------------------------------------------------------------------------------------------


def test_profile_reads_identity_from_context(demo_ctx):
    payload = get_user_profile(demo_ctx)
    assert payload["user"]["email"] == DEMO_EMAIL
    assert payload["user"]["org"] == "sales"


def test_missing_user_id_is_a_clear_error():
    for tool_call in (
        lambda c: get_user_profile(c),
        lambda c: get_user_assets(c),
        lambda c: create_ticket(c, "x", "y", "software"),
        lambda c: search_similar_tickets(c, "anything"),
        lambda c: place_catalog_order(c, str(uuid.uuid4()), []),
    ):
        payload = tool_call(ctx_for(None))
        assert "no acting user" in payload["error"]


def test_unknown_user_is_a_clear_error():
    payload = get_user_profile(ctx_for("nobody@corp.com"))
    assert "not found" in payload["error"]


def test_assets_scoped_to_acting_user(demo_ctx, demo_user_id):
    payload = get_user_assets(demo_ctx)
    assert payload["assets"], "demo user should own at least one asset"
    with SessionLocal() as s:
        for a in payload["assets"]:
            assert s.get(Asset, uuid.UUID(a["asset_id"])).user_id == demo_user_id


# ---------------------------------------------------------------------------------------------
# Ticket tools: create / update / comment / dedup search
# ---------------------------------------------------------------------------------------------


def test_create_ticket_embeds_at_creation(demo_ctx, demo_user_id, clean_writes):
    payload = create_ticket(
        demo_ctx,
        title="Test VPN drops on corporate wifi",
        description="VPN disconnects every few minutes when on the office wifi.",
        category="network",
        priority="high",
    )
    ticket_id = uuid.UUID(payload["ticket"]["ticket_id"])
    with SessionLocal() as s:
        ticket = s.get(Ticket, ticket_id)
        assert ticket.user_id == demo_user_id  # created for the RIGHT user
        assert ticket.embedding is not None  # embedded at creation (invariant 3)
        assert ticket.status == TicketStatus.open


def test_create_ticket_rejects_foreign_asset(demo_ctx, other_user_asset, clean_writes):
    payload = create_ticket(
        demo_ctx,
        title="Test broken laptop",
        description="screen cracked",
        category="hardware",
        asset_id=str(other_user_asset),
    )
    assert "does not belong" in payload["error"]


def test_create_ticket_rejects_malformed_asset_id(demo_ctx, clean_writes):
    payload = create_ticket(
        demo_ctx, title="t", description="d", category="hardware", asset_id="my laptop"
    )
    assert "invalid asset_id" in payload["error"]


def test_create_ticket_rejects_invalid_enum_without_crashing(demo_ctx, clean_writes):
    payload = create_ticket(demo_ctx, title="t", description="d", category="parking")
    assert "invalid category" in payload["error"]


def test_update_own_ticket(demo_ctx, clean_writes):
    created = create_ticket(
        demo_ctx, title="Test printer jam", description="tray 2 jams", category="hardware"
    )
    payload = update_ticket(
        demo_ctx, created["ticket"]["ticket_id"], status="resolved", priority="low"
    )
    assert payload["ticket"]["status"] == "resolved"
    assert payload["ticket"]["priority"] == "low"


def test_update_foreign_ticket_is_refused(demo_ctx, demo_user_id):
    with SessionLocal() as s:
        foreign = s.scalar(select(Ticket.id).where(Ticket.user_id != demo_user_id).limit(1))
    payload = update_ticket(demo_ctx, str(foreign), status="closed")
    assert "does not belong" in payload["error"]


def test_update_ticket_rejects_invalid_enum(demo_ctx):
    payload = update_ticket(demo_ctx, str(uuid.uuid4()), status="finished")
    assert "invalid status" in payload["error"]


def test_comment_on_foreign_ticket_is_allowed_for_dedup(demo_ctx, demo_user_id, clean_writes):
    # Deliberate: dedup means "me too" comments land on OTHER users' tickets.
    with SessionLocal() as s:
        foreign = s.scalar(select(Ticket.id).where(Ticket.user_id != demo_user_id).limit(1))
    payload = add_ticket_comment(demo_ctx, str(foreign), "Also affecting demo.user@corp.com.")
    assert payload["comment"]["ticket_id"] == str(foreign)


def test_comment_on_missing_ticket_errors(demo_ctx):
    payload = add_ticket_comment(demo_ctx, str(uuid.uuid4()), "hello?")
    assert "not found" in payload["error"]


# --- get_ticket_status (M8, built for the MCP surface — ADR-040) ------------------------------


def test_ticket_status_own_ticket(demo_ctx, demo_user_id):
    with SessionLocal() as s:
        own = s.scalars(select(Ticket).where(Ticket.user_id == demo_user_id).limit(1)).one()
    payload = get_ticket_status(demo_ctx, str(own.id))
    ticket = payload["ticket"]
    assert ticket["ticket_id"] == str(own.id)
    assert ticket["status"] == own.status
    assert {"title", "priority", "category", "comment_count", "latest_comment"} <= ticket.keys()


def test_ticket_status_foreign_ticket_is_refused(demo_ctx, demo_user_id):
    # Unlike add_ticket_comment (foreign allowed for dedup), status READS are ownership-gated:
    # MCP exposes this to external clients, and other users' tickets are an information leak.
    with SessionLocal() as s:
        foreign = s.scalar(select(Ticket.id).where(Ticket.user_id != demo_user_id).limit(1))
    payload = get_ticket_status(demo_ctx, str(foreign))
    assert "does not belong" in payload["error"]


def test_ticket_status_guards_format_and_existence(demo_ctx):
    assert "expected a UUID" in get_ticket_status(demo_ctx, "my latest ticket")["error"]
    assert "not found" in get_ticket_status(demo_ctx, str(uuid.uuid4()))["error"]


def test_ticket_status_requires_identity():
    payload = get_ticket_status(ctx_for(None), str(uuid.uuid4()))
    assert "no acting user" in payload["error"]


# --- post_slack_message (M8, ADR-039): destination from context, graceful degradation ---------


def slack_ctx(**overrides) -> RunContextWrapper[ChatContext]:
    fields = {
        "user_id": DEMO_EMAIL,
        "source": "slack",
        "slack_channel": "C123",
        "slack_thread_ts": "1700000000.000100",
    }
    fields.update(overrides)
    return RunContextWrapper(context=ChatContext(**fields))


def test_post_slack_message_refuses_outside_slack_thread(demo_ctx):
    # The chat path has no thread — the LLM cannot conjure a destination (never an argument).
    payload = post_slack_message(demo_ctx, "hello thread")
    assert "not a Slack conversation" in payload["error"]


def test_post_slack_message_sink_seam_captures_instead_of_sending(monkeypatch, tmp_path):
    sink = tmp_path / "slack_sink.jsonl"
    monkeypatch.setattr(get_settings(), "slack_sink_file", str(sink))
    payload = post_slack_message(slack_ctx(), "ticket ABC linked")
    assert payload["slack_message"]["posted"] is True
    import json

    line = json.loads(sink.read_text().splitlines()[0])
    assert line == {
        "channel": "C123",
        "thread_ts": "1700000000.000100",
        "text": "ticket ABC linked",
    }


def test_post_slack_message_noops_without_credentials(monkeypatch):
    # CI / local dev run Slack-less: a logged error dict, never a crash (M8 requirement 2).
    monkeypatch.setattr(get_settings(), "slack_sink_file", "")
    monkeypatch.setattr(get_settings(), "slack_bot_token", "")
    payload = post_slack_message(slack_ctx(), "ticket ABC created")
    assert "not configured" in payload["error"]


def test_similar_tickets_finds_exact_seeded_duplicate(demo_ctx):
    # The searched ticket must come back flagged at ~1.0 — but not necessarily FIRST: the
    # seeded data contains verbatim-duplicate tickets (same title+description), and the tie
    # between identical embeddings orders arbitrarily. Surfaced when migration 0004's
    # backfill UPDATE rewrote physical row order; heap order was never a contract.
    with SessionLocal() as s:
        seeded = s.scalars(
            select(Ticket).where(Ticket.status != TicketStatus.closed).limit(1)
        ).one()
        expected_id, text = str(seeded.id), f"{seeded.title}\n\n{seeded.description}"
    payload = search_similar_tickets(demo_ctx, text)
    top = payload["candidates"][0]
    assert top["similarity"] > 0.99  # same text, same embedding space as ingest
    assert top["likely_duplicate"] is True
    exact = {c["ticket_id"] for c in payload["candidates"] if c["similarity"] > 0.99}
    assert expected_id in exact


def test_similar_tickets_excludes_closed_by_default(demo_ctx):
    payload = search_similar_tickets(demo_ctx, "laptop will not turn on", top_k=20)
    assert payload["candidates"]
    assert all(c["status"] != "closed" for c in payload["candidates"])
    with_closed = search_similar_tickets(
        demo_ctx, "laptop will not turn on", top_k=200, include_closed=True
    )
    assert any(c["status"] == "closed" for c in with_closed["candidates"])


# ---------------------------------------------------------------------------------------------
# Catalog tools: listing, placement, and the >$500 HITL gate
# ---------------------------------------------------------------------------------------------


def test_catalog_os_filter(demo_ctx):
    all_items = list_catalog_items()["items"]
    macos_items = list_catalog_items(os_filter="macos")["items"]
    assert 0 < len(macos_items) < len(all_items)
    with SessionLocal() as s:
        for i in macos_items:
            compat = s.get(CatalogItem, uuid.UUID(i["item_id"])).os_compat
            assert compat is None or "macos" in compat


def test_catalog_flags_items_needing_approval():
    items = list_catalog_items()["items"]
    threshold = get_settings().hitl_approval_threshold_usd
    assert all(i["requires_approval"] == (i["price_usd"] > threshold) for i in items)


def test_catalog_rejects_invalid_os_filter():
    assert "invalid os_filter" in list_catalog_items(os_filter="beos")["error"]


def test_cheap_order_places_immediately(demo_ctx, demo_user_id, clean_writes):
    cheap, _ = _cheap_and_pricey()
    payload = place_catalog_order(demo_ctx, cheap["item_id"], _form_values_for(cheap))
    order = payload["order"]
    assert order["status"] == OrderStatus.submitted
    assert order["approval_state"] == ApprovalState.not_required
    with SessionLocal() as s:
        assert s.get(Order, uuid.UUID(order["order_id"])).user_id == demo_user_id


def test_pricey_order_forces_pending_approval(demo_ctx, clean_writes):
    _, pricey = _cheap_and_pricey()
    draft = place_catalog_order(demo_ctx, pricey["item_id"], _form_values_for(pricey))
    assert draft["order"]["status"] == OrderStatus.draft  # NOT placed
    assert "request_approval" in draft["next_step"]
    pending = request_approval(demo_ctx, draft["order"]["order_id"])
    assert pending["order"]["status"] == OrderStatus.submitted
    assert pending["order"]["approval_state"] == ApprovalState.pending  # the M0 CHECK shape


def test_order_price_comes_from_db_not_llm(demo_ctx, clean_writes):
    # There is no price argument to lie through; the payload price is the catalog row's.
    cheap, _ = _cheap_and_pricey()
    payload = place_catalog_order(demo_ctx, cheap["item_id"], _form_values_for(cheap))
    assert payload["order"]["price_usd"] == cheap["price_usd"]


def test_order_rejects_unknown_item_and_bad_uuid(demo_ctx):
    assert "not found" in place_catalog_order(demo_ctx, str(uuid.uuid4()), [])["error"]
    assert "invalid item_id" in place_catalog_order(demo_ctx, "a MacBook", [])["error"]


def test_order_validates_form_against_schema(demo_ctx, clean_writes):
    cheap, _ = _cheap_and_pricey()
    if not any(f.get("required") for f in cheap["form_schema"]):
        pytest.skip("cheapest item has no required form fields")
    missing = place_catalog_order(demo_ctx, cheap["item_id"], [])
    assert "missing required form fields" in missing["error"]
    unknown = place_catalog_order(
        demo_ctx,
        cheap["item_id"],
        _form_values_for(cheap) + [FormValue(name="gift_wrap", value="yes")],
    )
    assert "unknown form fields" in unknown["error"]


def test_get_my_orders_scoped_to_acting_user_with_current_state(
    demo_ctx, demo_user_id, clean_writes
):
    # A fresh order + an out-of-band approval (the manager path, fresh process by design):
    # the tool must report the POST-approval state — the exact staleness bug this tool fixes.
    _, pricey = _cheap_and_pricey()
    draft = place_catalog_order(demo_ctx, pricey["item_id"], _form_values_for(pricey))
    request_approval(demo_ctx, draft["order"]["order_id"])
    approve_order(draft["order"]["order_id"])

    payload = get_my_orders(demo_ctx)
    assert "results" not in payload  # bare key would leak into _collect_citations
    orders = {o["order_id"]: o for o in payload["orders"]}
    approved = orders[draft["order"]["order_id"]]
    assert approved["approval_state"] == ApprovalState.approved
    assert approved["summary"] == "approved by the manager — order placed"
    with SessionLocal() as s:
        foreign = set(map(str, s.scalars(select(Order.id).where(Order.user_id != demo_user_id))))
    assert not foreign & set(orders)  # never another user's orders


def test_get_my_orders_requires_identity():
    assert "no acting user" in get_my_orders(ctx_for(None))["error"]


# ---------------------------------------------------------------------------------------------
# Record numbers (ADR-046): DB-assigned, ascending, and accepted as tool arguments
# ---------------------------------------------------------------------------------------------


def test_record_numbers_assigned_ascending_and_returned(demo_ctx, clean_writes):
    first = create_ticket(demo_ctx, "Test number seq A", "x", "other")["ticket"]
    second = create_ticket(demo_ctx, "Test number seq B", "x", "other")["ticket"]
    assert re.fullmatch(r"TKT\d{3,}", first["number"])
    assert re.fullmatch(r"TKT\d{3,}", second["number"])
    assert int(second["number"][3:]) == int(first["number"][3:]) + 1

    cheap, _ = _cheap_and_pricey()
    order = place_catalog_order(demo_ctx, cheap["item_id"], _form_values_for(cheap))["order"]
    assert re.fullmatch(r"ORD\d{3,}", order["number"])


def test_ticket_tools_accept_the_user_facing_number(demo_ctx, clean_writes):
    created = create_ticket(demo_ctx, "Test number lookup", "x", "other")["ticket"]
    number = created["number"]
    assert get_ticket_status(demo_ctx, number)["ticket"]["ticket_id"] == created["ticket_id"]
    assert get_ticket_status(demo_ctx, number.lower())["ticket"]["number"] == number
    updated = update_ticket(demo_ctx, number, status="resolved")
    assert updated["ticket"]["status"] == TicketStatus.resolved
    assert "not found" in get_ticket_status(demo_ctx, "TKT999999")["error"]


def test_ticket_number_lookup_still_enforces_ownership(demo_ctx, demo_user_id):
    with SessionLocal() as s:
        foreign = s.scalar(select(Ticket.number).where(Ticket.user_id != demo_user_id).limit(1))
    assert "does not belong" in get_ticket_status(demo_ctx, foreign)["error"]


def test_record_detail_functions_resolve_number_and_uuid(demo_ctx, clean_writes):
    created = create_ticket(demo_ctx, "Test detail page", "with a body", "other")["ticket"]
    add_ticket_comment(demo_ctx, created["number"], "first activity entry")
    by_number = get_ticket_details(created["number"])
    by_uuid = get_ticket_details(created["ticket_id"])
    assert by_number == by_uuid
    assert by_number["title"] == "Test detail page"
    assert [c["body"] for c in by_number["comments"]] == ["first activity entry"]
    assert get_ticket_details("TKT999999") is None
    assert get_ticket_details("garbage") is None

    cheap, _ = _cheap_and_pricey()
    order = place_catalog_order(demo_ctx, cheap["item_id"], _form_values_for(cheap))["order"]
    detail = get_order_details(order["number"])
    assert detail["item"] == cheap["name"]
    assert detail["summary"] == "placed (no approval needed)"
    assert get_order_details("ORD999999") is None


def test_request_approval_accepts_the_order_number(demo_ctx, clean_writes):
    _, pricey = _cheap_and_pricey()
    draft = place_catalog_order(demo_ctx, pricey["item_id"], _form_values_for(pricey))["order"]
    pending = request_approval(demo_ctx, draft["number"])
    assert pending["order"]["approval_state"] == ApprovalState.pending
    assert pending["order"]["number"] == draft["number"]


def test_order_summary_covers_the_state_machine():
    def order_in(status, approval_state):
        return Order(status=status, approval_state=approval_state)

    cases = {
        (OrderStatus.draft, ApprovalState.not_required): "draft — not yet submitted",
        (OrderStatus.submitted, ApprovalState.pending): "awaiting manager approval",
        (OrderStatus.submitted, ApprovalState.approved): "approved by the manager — order placed",
        (OrderStatus.submitted, ApprovalState.not_required): "placed (no approval needed)",
        (OrderStatus.fulfilled, ApprovalState.not_required): "fulfilled — delivered/provisioned",
        (OrderStatus.cancelled, ApprovalState.rejected): "rejected by the manager and cancelled",
    }
    for (status, approval_state), expected in cases.items():
        assert _order_summary(order_in(status, approval_state)) == expected


def test_request_approval_on_foreign_order_is_refused(demo_ctx, demo_user_id):
    with SessionLocal() as s:
        foreign = s.scalar(select(Order.id).where(Order.user_id != demo_user_id).limit(1))
    payload = request_approval(demo_ctx, str(foreign))
    assert "does not belong" in payload["error"]


def test_request_approval_is_idempotent_on_pending(demo_ctx):
    # The seeded $650 Photoshop order is the demo user's and already pending.
    with SessionLocal() as s:
        pending = s.scalar(
            select(Order.id)
            .join(User, User.id == Order.user_id)
            .where(User.email == DEMO_EMAIL, Order.approval_state == ApprovalState.pending)
        )
    payload = request_approval(demo_ctx, str(pending))
    assert payload["note"] == "already awaiting approval"
    assert payload["order"]["approval_state"] == ApprovalState.pending


# ---------------------------------------------------------------------------------------------
# Human approval path (not agent tools): approve places, reject cancels — across processes
# ---------------------------------------------------------------------------------------------


def _fresh_pending_order(demo_ctx) -> str:
    _, pricey = _cheap_and_pricey()
    draft = place_catalog_order(demo_ctx, pricey["item_id"], _form_values_for(pricey))
    return request_approval(demo_ctx, draft["order"]["order_id"])["order"]["order_id"]


def test_approve_places_the_order(demo_ctx, clean_writes):
    order_id = _fresh_pending_order(demo_ctx)
    assert any(o["order_id"] == order_id for o in list_pending_orders())
    approved = approve_order(order_id)
    assert approved["approval_state"] == ApprovalState.approved
    assert approved["status"] == OrderStatus.submitted  # placed, no longer blocked
    assert not any(o["order_id"] == order_id for o in list_pending_orders())


def test_reject_cancels_the_order(demo_ctx, clean_writes):
    order_id = _fresh_pending_order(demo_ctx)
    rejected = reject_order(order_id)
    assert rejected["approval_state"] == ApprovalState.rejected
    assert rejected["status"] == OrderStatus.cancelled  # CHECK: pending can't outlive submitted


def test_approve_requires_pending(demo_ctx, clean_writes):
    order_id = _fresh_pending_order(demo_ctx)
    reject_order(order_id)
    assert "not pending" in approve_order(order_id)["error"]

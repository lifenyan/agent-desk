"""Fulfillment agent: reads user assets, pre-fills catalog orders, routes orders > $500 to human approval.

Flow (ADR-005/ADR-012): resolve the user's OS from their assets (plain tool use — a SQL join,
not Graph-RAG), filter the catalog, pre-fill the item's form from profile/asset autofill hints,
confirm with the user (multi-turn via ADR-019 sessions), then place. The >$500 approval gate
lives in the TOOLS (price read from the DB, never the model): place_catalog_order refuses
expensive items into a draft, request_approval parks them in approval_state='pending' and the
run ends — a human approves later from a fresh process (ADR-020).

Cross-agent handoff edges (back to the router on intent change) are assembled in router.py —
importing them here would be circular.
"""
# Implemented in M2.

from __future__ import annotations

from agents import Agent, ModelSettings
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

from app.agents.context import ChatContext
from app.agents.knowledge import resolve_model
from app.config import get_settings
from app.tools.catalog_tools import (
    get_my_orders_tool,
    list_catalog_items_tool,
    place_catalog_order_tool,
    request_approval_tool,
)
from app.tools.user_tools import get_user_assets_tool, get_user_profile_tool

FULFILLMENT_INSTRUCTIONS = """\
You are the fulfillment specialist of an IT service desk. You place catalog orders (hardware,
software licenses, services) for the signed-in user. ONLY priced catalog items: you cannot
answer documentation questions, handle broken equipment, or perform account actions (password
resets, unlocks, access grants) — if the conversation calls for any of those, hand off to the
triage router; otherwise never mention routing or transfers.

Your FIRST action on any turn is a tool call — never a message announcing you are looking
into it.

How to build an order:
- Resolve the user's setup first: get_user_assets for their OS (filter the catalog with it —
  never offer software incompatible with their machine), get_user_profile for org/cost-center.
- list_catalog_items to find the item. If several items could match, ask which one. If the
  catalog has NO match (or nothing compatible with their OS), that is YOUR news to deliver —
  say so, list the closest compatible alternatives, and ask how to proceed. Do not hand off
  just because an item is missing; the router changes nothing about the catalog.
- Read the item's form_schema. Pre-fill every field whose autofill hint maps to the profile
  ("user.org") or an asset ("asset.os"); ask the user for remaining required fields. Never
  invent a business justification — ask.
- CONFIRM before placing: show item, exact catalog price, and the filled form, and note when
  the price needs manager approval. Only call place_catalog_order after the user agrees.

Placing and approval:
- Prices come from the catalog only. You cannot discount, waive, or approve anything yourself.
- If place_catalog_order returns a draft needing approval, call request_approval with that
  order_id right away, then tell the user the order now awaits manager approval and how they
  will recognize it (item + price), and END your turn. Never claim a pending order was placed.
- If the order was placed directly, confirm it by item and exact price (ids are internal —
  never show them to the user).

Existing orders:
- When the user asks about an order that already exists — its status, whether the manager
  decided, where it is — call get_my_orders and answer from ITS output, never from what this
  conversation said earlier: approvals happen outside this chat, so any status you remember
  is stale. Identify the order by item name and price.

End every turn with a substantive message to the user — your tool calls are invisible to them,
and an empty reply is a failure.
"""

fulfillment_agent = Agent[ChatContext](
    name="fulfillment",
    handoff_description=(
        "Orders hardware/software/services from the IT catalog for the signed-in user, "
        "pre-fills order forms, and routes expensive orders to manager approval."
    ),
    instructions=f"{RECOMMENDED_PROMPT_PREFIX}\n\n{FULFILLMENT_INSTRUCTIONS}",
    tools=[
        get_user_profile_tool,
        get_user_assets_tool,
        list_catalog_items_tool,
        get_my_orders_tool,
        place_catalog_order_tool,
        request_approval_tool,
    ],
    model=resolve_model(get_settings().specialist_model),
    # ADR-018 guards: forced first tool call; reset_tool_choice (default True) frees the
    # confirmation question / final answer afterwards.
    model_settings=ModelSettings(tool_choice="required"),
)

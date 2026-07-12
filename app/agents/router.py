"""Tool-less router agent: classifies query intent and hands off to knowledge, fulfillment, or incident agent.

This module is also the GRAPH ASSEMBLY POINT: the cross-agent edges (knowledge→incident for
the confirmed ticket offer, specialist→router for mid-conversation intent changes) are wired
here after all agents exist — the specialists cannot import each other or the router without
an import cycle. Anything that runs agents imports this module (routes_chat, evals, tests),
so the full graph is always materialized.

Edges (ADR-003):
    router     → knowledge | fulfillment | incident      (the only job of the router)
    knowledge  → incident                                (user accepted the refusal ticket offer)
    knowledge  → router  |  fulfillment → router  |  incident → router
                                                         (ONLY on genuine intent change)
"""
# Implemented in M1 with handoffs=[knowledge]; M2 added fulfillment + incident and the
# cross-agent edges above.

from __future__ import annotations

from agents import Agent, ModelSettings
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

from app.agents.context import ChatContext
from app.agents.fulfillment import fulfillment_agent
from app.agents.guardrails import slack_injection_guardrail
from app.agents.incident import incident_agent
from app.agents.knowledge import knowledge_agent, resolve_model
from app.config import get_settings

# ADR-003: the router carries ZERO tools — its only job is intent classification + handoff,
# reading each specialist's handoff_description. Giving it tools makes it half-answer instead
# of routing (the failure mode this architecture exists to avoid).
ROUTER_INSTRUCTIONS = """\
You are the triage router of an IT service desk. Classify the user's intent and hand off to
the matching specialist. Your ONLY output is a handoff: never answer the user, never send a
message announcing the transfer (handoffs are invisible to the user), and do not ask clarifying
questions unless the intent is genuinely ambiguous.

- knowledge — questions answerable from documentation: how-tos, policies, product info,
  release notes, troubleshooting guidance ("how do I…", "what's the policy on…", "what's new
  in v5.2").
- fulfillment — the user wants to GET a PRICED CATALOG ITEM: order/purchase/request hardware,
  software licenses, or services ("I need a new laptop", "order me Photoshop"). Route the
  acquisition itself to fulfillment even though the knowledge base documents how ordering
  works — fulfillment can actually place the order, which beats instructions about it.
  Questions about an EXISTING catalog order ("what's the status of my order?", "was my
  Tableau license approved?") also go here — orders are fulfillment's domain end to end.
- incident — something is broken, failing, or not working and needs IT to act ("my VPN keeps
  dropping", "I can't log in"), or the user manages an existing ticket of theirs. Reports
  ingested from a Slack thread (the message says so explicitly) ALWAYS go here — they exist
  to become tickets, whatever the thread chatter looks like.

Routing judgment:
- Account actions — password resets, unlocks, access/permission requests — are NEVER catalog
  orders. "Reset my password" / "how do I reset my password" → knowledge (it is a documented
  self-service flow); an account that stays broken after that → incident.
- "X is broken, how do I fix it?" is knowledge when the user wants instructions, incident when
  they want IT to take over. For FIXES, prefer knowledge when either reading applies — a
  documented self-service fix is faster than a ticket, and the knowledge agent can escalate.
  (This preference is for troubleshooting only — it never redirects an acquisition away from
  fulfillment.)
- If one message asks for several things in different domains, route to the specialist of the
  FIRST actionable request; specialists return to you when the topic changes.
- In an ongoing conversation, keep routing follow-ups ("yes, go ahead") to the specialist
  already handling the flow. A status question about the thing just handled belongs to that
  same specialist WHATEVER the user calls it — "what's the status of my ticket?" right after
  placing an order is an order-status question (→ fulfillment), not a ticket lookup.
"""

router_agent = Agent[ChatContext](
    name="router",
    instructions=f"{RECOMMENDED_PROMPT_PREFIX}\n\n{ROUTER_INSTRUCTIONS}",
    handoffs=[knowledge_agent, fulfillment_agent, incident_agent],
    model=resolve_model(get_settings().triage_model),
    # M8 injection screen (ADR-041): input guardrails only run on the FIRST agent of a run,
    # and every routes_chat run starts here. The guardrail no-ops instantly unless the run
    # context says source="slack", so the interactive chat path pays nothing.
    input_guardrails=[slack_injection_guardrail],
    # Force the handoff (ADR-018): a tool-less router with tool_choice="required" must emit one
    # of its handoffs rather than narrate "Routing to knowledge…" and end the turn.
    model_settings=ModelSettings(tool_choice="required"),
    # …and keep forcing it on every later acting turn (ADR-022): the SDK counts a HANDOFF as
    # tool use, so the default reset_tool_choice=True would flip the router to "auto" after its
    # first handoff — and when a specialist back-edges mid-run, an "auto" router can emit text
    # (observed live: an empty final message = a non-answer). The router never speaks, so tool
    # choice must never reset. Loop safety comes from the specialists' restricted back-edge
    # instructions + max_turns, measured by the routing suite's ping-pong metric.
    reset_tool_choice=False,
)

# --- cross-agent edges (see module docstring; wired post-construction to avoid import cycles) ---
knowledge_agent.handoffs.extend([incident_agent, router_agent])
fulfillment_agent.handoffs.append(router_agent)
incident_agent.handoffs.append(router_agent)

"""Structural invariants for the agent graph (router + knowledge + fulfillment + incident).

These are fast, deterministic, LLM-free config assertions — they pin the wiring that the
architecture depends on, so a future refactor can't silently drop it:
- ADR-003: the router is tool-less and routes via handoffs; the M2 edges are exactly
  router→{knowledge,fulfillment,incident}, knowledge→incident (confirmed ticket offer), and
  specialist→router back-edges.
- ADR-018: every agent carries the handoff prompt prefix and tool_choice="required" (+ the
  reset_tool_choice default) — the guards that fixed the flaky "narrate instead of act" handoff.
- Each specialist holds ONLY its own tools (ADR-004 discipline at the agent level).

Importing app.agents.router matters: it is the graph assembly point (cross-agent edges are
wired there). The BEHAVIORAL counterpart (routing accuracy, no ping-pong, real final answers)
needs a live model and lives in the M2 routing eval suite (evals/run_evals.py), NOT here.
"""
# Implemented in the M1 follow-up alongside ADR-018; extended to the full graph in M2.

from __future__ import annotations

import pytest
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

from app.agents.fulfillment import fulfillment_agent
from app.agents.incident import incident_agent
from app.agents.knowledge import knowledge_agent
from app.agents.router import router_agent
from app.config import Settings, get_settings

ALL_AGENTS = [router_agent, knowledge_agent, fulfillment_agent, incident_agent]


def _handoff_names(agent: Agent) -> set[str]:
    return {h.name for h in agent.handoffs}


def test_router_is_tool_less_and_hands_off_to_all_three_specialists():
    # ADR-003: zero tools; its only action is a handoff.
    assert list(router_agent.tools) == []
    assert _handoff_names(router_agent) == {"knowledge", "fulfillment", "incident"}


def test_knowledge_edges_incident_for_ticket_offer_plus_router_backedge():
    assert _handoff_names(knowledge_agent) == {"incident", "router"}


def test_specialist_backedges_to_router_only():
    assert _handoff_names(fulfillment_agent) == {"router"}
    assert _handoff_names(incident_agent) == {"router"}


@pytest.mark.parametrize("agent", ALL_AGENTS, ids=lambda a: a.name)
def test_adr018_guards_on_every_agent(agent: Agent):
    # ADR-018: forced tool call on the acting turn + multi-agent framing that forbids
    # narrating transfers.
    assert agent.model_settings.tool_choice == "required"
    assert agent.instructions.startswith(RECOMMENDED_PROMPT_PREFIX)


def test_m10_reasoning_effort_wired_from_config():
    # M10 (ADR-047): reasoning effort is config-driven — the router classifies at
    # router_reasoning_effort, fulfillment/incident act at specialist_reasoning_effort, and
    # knowledge carries its OWN setting (its ADR-017 stage-2 judgment is effort-sensitive).
    # The agents must reflect whatever config resolved to (env-overridable like every other
    # setting); the DEFAULTS are pinned separately below because the eval floors were
    # measured against them.
    settings = get_settings()
    assert router_agent.model_settings.reasoning.effort == settings.router_reasoning_effort
    assert knowledge_agent.model_settings.reasoning.effort == settings.knowledge_reasoning_effort
    for agent in (fulfillment_agent, incident_agent):
        assert agent.model_settings.reasoning.effort == settings.specialist_reasoning_effort


def test_m10_reasoning_effort_defaults_pinned():
    # The measured defaults (ADR-047): router "minimal" (pure classification, 0 reasoning
    # tokens, routing 30/30); fulfillment/incident "low" (all gates green); knowledge
    # "medium" — "low" measurably produced Sources-decorated refusals on the chat path,
    # violating the ADR-017 contract and leaking refusals into the semantic cache. Read off
    # the FIELD defaults, not get_settings(), so a local env override can't hide a drift.
    assert Settings.model_fields["router_reasoning_effort"].default == "minimal"
    assert Settings.model_fields["specialist_reasoning_effort"].default == "low"
    assert Settings.model_fields["knowledge_reasoning_effort"].default == "medium"


def test_reset_tool_choice_split():
    # Specialists reset after the first tool call so they can write the final answer (ADR-018);
    # the ROUTER never resets (ADR-022) — the SDK counts its handoff as tool use, and a reset
    # router reached via a back-edge can emit text (an empty non-answer, observed live). The
    # router never speaks, so it must stay forced onto its handoffs.
    assert router_agent.reset_tool_choice is False
    for agent in (knowledge_agent, fulfillment_agent, incident_agent):
        assert agent.reset_tool_choice is True


def test_knowledge_has_only_retrieval_tools():
    assert {t.name for t in knowledge_agent.tools} == {
        "search_knowledge_articles",
        "get_release_notes",
    }


def test_fulfillment_has_only_ordering_tools():
    # get_my_orders: deliberate — order-status follow-ups must be answered from the DB, not
    # conversation memory (approvals happen out-of-band; history is stale by design).
    assert {t.name for t in fulfillment_agent.tools} == {
        "get_user_profile",
        "get_user_assets",
        "list_catalog_items",
        "get_my_orders",
        "place_catalog_order",
        "request_approval",
    }


def test_incident_has_only_ticket_and_graph_tools():
    # M9 deliberately added query_dependency_graph (ADR-035): impact/blast-radius questions
    # are the incident agent's domain; the graph tool is read-only and user-independent.
    # M8 deliberately added the Slack reply pair (ADR-039): post_slack_message (destination
    # locked to the run context's thread) + search_knowledge_articles (the ONE suggested
    # article in the thread reply — instructions scope it to that step).
    # ADR-046 promoted get_ticket_status from MCP-only: users quote TKTnnn numbers and a
    # status question needs a direct ownership-gated read.
    assert {t.name for t in incident_agent.tools} == {
        "get_user_profile",
        "get_user_assets",
        "search_similar_tickets",
        "create_ticket",
        "get_ticket_status",
        "add_ticket_comment",
        "update_ticket",
        "query_dependency_graph",
        "search_knowledge_articles",
        "post_slack_message",
    }


def test_injection_guardrail_pinned_on_router_only():
    # ADR-041: input guardrails only run on a run's FIRST agent, and every routes_chat run
    # starts at the router — attaching it anywhere else is dead config a refactor could
    # mistake for coverage. run_in_parallel=False is load-bearing: the tripwire must fire
    # BEFORE any specialist tool call, not race it.
    names = [g.get_name() for g in router_agent.input_guardrails]
    assert names == ["slack_injection_guardrail"]
    assert all(not g.run_in_parallel for g in router_agent.input_guardrails)
    for agent in (knowledge_agent, fulfillment_agent, incident_agent):
        assert agent.input_guardrails == []


def test_no_agent_can_approve_orders():
    # ADR-005/ADR-020: approval authority is human-only — approve/reject exist as plain
    # functions for the approvals API and must never be handed to a model.
    for agent in ALL_AGENTS:
        assert not {t.name for t in agent.tools} & {"approve_order", "reject_order"}

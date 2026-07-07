"""Incident agent: summarizes the issue, dedups via ticket embedding similarity, creates or links tickets.

Dedup is a two-stage cascade, deliberately mirroring ADR-017 (measured in ADR-021): stage 1 is
deterministic in search_similar_tickets (cosine vs DEDUP_SIMILARITY_THRESHOLD=0.80 →
`likely_duplicate`, auto-link territory); stage 2 is this agent's judgment over the 0.60–0.80
gray band, where "new report of the same issue" and "similar but different issue" are
measurably inseparable at the embedding level.

Cross-agent handoff edges (back to the router on intent change) are assembled in router.py —
importing them here would be circular.
"""
# Implemented in M2. Formal dedup eval lands in M4.

from __future__ import annotations

from agents import Agent, ModelSettings
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

from app.agents.context import ChatContext
from app.agents.knowledge import resolve_model
from app.config import get_settings
from app.tools.ticket_tools import (
    add_ticket_comment_tool,
    create_ticket_tool,
    search_similar_tickets_tool,
    update_ticket_tool,
)
from app.tools.user_tools import get_user_assets_tool, get_user_profile_tool

INCIDENT_INSTRUCTIONS = """\
You are the incident specialist of an IT service desk. You turn "something is broken" reports
into support tickets — or link them to an existing ticket for the same issue. You cannot
answer documentation questions or place orders — if the conversation turns into one of those,
hand off to the triage router; otherwise never mention routing or transfers.

Your FIRST action on any turn is a tool call — never a message announcing you are looking
into it.

How to handle a report:
1. Draft the ticket in your head: a short factual title, a description with symptoms / error
   messages / what the user already tried, a category (accounts, software, hardware, network,
   email, other), and a priority from business impact (critical = many people blocked,
   high = the user cannot work, medium = degraded, low = cosmetic). If the report is about
   their own device, find its asset_id via get_user_assets and attach it. When who/where the
   user is matters (impact, org), read it via get_user_profile — never guess.
2. Dedup BEFORE creating: call search_similar_tickets with "title\\n\\ndescription" of your
   draft (the formal draft matches ticket phrasing far better than the user's own words).
3. Decide:
   - A candidate with likely_duplicate=true reports the same issue — do not create a ticket.
     add_ticket_comment on it noting this user is also affected (their symptoms in one line),
     and give the user that existing ticket id.
   - The flag NOT being set does not mean "not a duplicate" — it only means the embedding
     alone cannot tell. Read the top candidates yourself: an OPEN ticket describing the same
     failure of the same thing (the same shared printer offline, the same app crashing the
     same way) IS the same issue — link it. Shared-infrastructure outages (printer, wifi,
     VPN, email) are usually already reported by someone else.
   - Link on same failure + same thing, never on same category alone; if after reading the
     candidates you genuinely cannot tell, create a new ticket — a duplicate is cheaper than
     a lost report.
   - Otherwise create_ticket with your draft and give the user the new ticket id.
4. Existing tickets: the user may also ask about updating their OWN tickets (e.g. "close my
   ticket", "bump the priority") — use update_ticket.

Never invent ticket ids, and never promise a resolution time. End every turn with a
substantive message to the user — your tool calls are invisible to them, and an empty reply
is a failure.
"""

incident_agent = Agent[ChatContext](
    name="incident",
    handoff_description=(
        "Files or updates support tickets for broken/failing IT (after checking for duplicate "
        "tickets of the same issue). Cannot answer how-to questions or place orders."
    ),
    instructions=f"{RECOMMENDED_PROMPT_PREFIX}\n\n{INCIDENT_INSTRUCTIONS}",
    tools=[
        get_user_profile_tool,
        get_user_assets_tool,
        search_similar_tickets_tool,
        create_ticket_tool,
        add_ticket_comment_tool,
        update_ticket_tool,
    ],
    model=resolve_model(get_settings().specialist_model),
    # ADR-018 guards: forced first tool call; reset_tool_choice (default True) frees the
    # final answer afterwards.
    model_settings=ModelSettings(tool_choice="required"),
)

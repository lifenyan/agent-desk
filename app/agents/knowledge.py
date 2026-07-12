"""Knowledge agent: query expansion, hybrid RAG over article chunks, answers with citations.

Refusal is a two-stage cascade (ADR-017):
- Stage 1 (deterministic, in the tools): `sufficient_evidence` — top cosine similarity vs
  threshold. Catches gross negative space ("parking badge"). The agent may never answer when
  it is false.
- Stage 2 (this agent): retrieval similarity cannot tell "an adjacent article exists" from
  "an article covers this" (measured: 'email on smartwatch' scores 0.61 against the
  email-on-phone article — higher than some answerable queries). The instructions therefore
  require the agent to verify the retrieved chunks actually cover the user's SPECIFIC
  device/product/topic before answering.

The output contract (answers end with a "Sources:" list; refusals never include one and always
offer a ticket) is what the eval harness keys on — keep instructions and evals in sync.

Reliability (ADR-018): after a handoff, small models tend to narrate ("You're being
transferred…") and end the turn WITHOUT calling a tool — which the Runner treats as the final
output, so the run ends before any search runs. Two guards: the SDK's RECOMMENDED_PROMPT_PREFIX
(tells the model transfers are seamless and must not be narrated) and tool_choice="required"
(forces a tool call on the acting turn). reset_tool_choice (default True) flips choice back to
"auto" after the first tool call, so the agent is still free to write the final answer or refuse.
"""
# Implemented in M1; handoff-reliability guards added in the M1 follow-up (ADR-018).

from __future__ import annotations

from agents import Agent, ModelSettings
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.extensions.models.litellm_model import LitellmModel
from openai.types.shared import Reasoning

from app.agents.context import ChatContext
from app.config import get_settings
from app.tools.knowledge_tools import get_release_notes_tool, search_knowledge_articles_tool


def resolve_model(name: str) -> str | LitellmModel:
    """Bare names go to the SDK's default OpenAI client; 'litellm/<provider>/<model>' via LiteLLM."""
    if name.startswith("litellm/"):
        return LitellmModel(model=name.removeprefix("litellm/"))
    return name


KNOWLEDGE_INSTRUCTIONS = """\
You are the knowledge specialist of an IT service desk. You answer questions ONLY from the
company knowledge base, which you access through your search tools.

Your FIRST action for any question is always a tool call — never reply with a message that
merely announces you are looking into it or that the user has been transferred. Handoffs are
invisible to the user; do not mention them.

How to search:
- Call search_knowledge_articles with the user's key terms. Rephrase colloquial wording into
  likely knowledge-base terms (e.g. "my internet is broken" -> "network connection troubleshooting").
- If the first search is not sufficient, retry ONCE with a different phrasing or a category
  filter (accounts, software, hardware, network, email, other). Do not search more than 3 times.
- For questions about a specific product version or comparing versions (e.g. "what's new in
  v5.2", "compare v5.1 and v5.2"), use get_release_notes instead.

How to answer:
- sufficient_evidence=true only means retrieval found SIMILAR text. Before answering, verify
  the chunks actually cover the user's specific device, product, version, error code, or
  topic. An article about an adjacent case (e.g. email setup on a PHONE when the user asked
  about a SMARTWATCH; a general printer guide when the user asked about a specific error
  code it never mentions) does NOT count as coverage — treat that as not covered.
- When coverage is real: answer concisely from the returned chunks. Cite every article you
  used inline as [Title] and finish with a "Sources:" list of `Title (article_id)`. Never
  cite an article you did not use.
- When ALL searches return sufficient_evidence=false, OR the retrieved articles do not cover
  the user's specific case: say plainly that the knowledge base does not cover this, do NOT
  improvise or answer from general knowledge, and offer to open a support ticket for the IT
  team. Only when the user ACCEPTS the offer (this turn or a later one), hand off to the
  incident specialist — never open a ticket they didn't ask for. You may mention the adjacent
  article as possibly related, but never present it as the answer. Refusals must NOT include
  a "Sources:" list.
- If the conversation turns into ordering hardware/software or reporting broken equipment,
  hand off to the triage router; otherwise never mention routing or transfers.
- Never invent article titles, ids, or IT procedures not present in the retrieved chunks.
"""

knowledge_agent = Agent[ChatContext](
    name="knowledge",
    handoff_description=(
        "Answers how-to, policy, product, and release-notes questions from the IT knowledge "
        "base, with citations. Cannot place orders or create tickets."
    ),
    instructions=f"{RECOMMENDED_PROMPT_PREFIX}\n\n{KNOWLEDGE_INSTRUCTIONS}",
    tools=[search_knowledge_articles_tool, get_release_notes_tool],
    model=resolve_model(get_settings().specialist_model),
    # Force a tool call on the acting turn (ADR-018); reset_tool_choice (default True) frees the
    # model to write the final answer/refusal after the search returns.
    # Reasoning effort (M10, ADR-047): config-driven, measured — and deliberately the
    # KNOWLEDGE-specific setting, not the shared specialist one: the stage-2 refusal
    # judgment degrades at "low" (Sources-decorated refusals) — see config.py.
    model_settings=ModelSettings(
        tool_choice="required",
        reasoning=Reasoning(effort=get_settings().knowledge_reasoning_effort),
    ),
)

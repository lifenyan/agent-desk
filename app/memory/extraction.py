"""End-of-session fact extraction: LLM extracts durable user facts from the transcript, dedups against existing facts."""
# Implemented in M5 (ADR-031). Runs POST-RESPONSE (FastAPI BackgroundTasks in routes_chat), so
# the reply is never blocked and an extraction failure is a logged non-event, never a 500.
# One cheap structured-output call per chat turn on the triage model (gpt-5-mini); the merge
# rule itself lives in user_facts.apply_extracted_facts, LLM-free and unit-tested.

from __future__ import annotations

import logging
from functools import lru_cache

from agents import Agent, ModelSettings, RunConfig, Runner
from pydantic import BaseModel, Field

from app.agents.knowledge import resolve_model
from app.config import get_settings
from app.memory.user_facts import FactCandidate, apply_extracted_facts, get_user_facts

logger = logging.getLogger(__name__)

EXTRACTION_INSTRUCTIONS = """\
You extract DURABLE facts about a user from one message they sent to an IT service desk.
A durable fact is stable, personal context that would still matter in a conversation weeks
from now: their device/OS, org/team/role, working patterns (travel, remote, on-call), or
stated preferences (contact channel, language, accessibility needs).

Do NOT extract: the current issue or request itself, anything about other people or the
company, one-off details (ticket numbers, error codes, order justifications), or anything
already covered by an existing fact below unless the user CONTRADICTS it.

Rules:
- Most messages contain no durable fact: return an empty list. When unsure, leave it out.
- fact_type is a short snake_case category key (device_os, org, contact_preference, ...).
  When updating an existing fact, REUSE its exact fact_type so the update replaces it.
- fact is one self-contained sentence; confidence in [0,1] reflects how explicit the user was
  (stated outright ~0.9, strongly implied ~0.6).
"""


class ExtractedFact(BaseModel):
    fact_type: str = Field(description="short snake_case category key, e.g. device_os")
    fact: str = Field(description="one self-contained sentence")
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    facts: list[ExtractedFact]


@lru_cache
def _extractor() -> Agent:
    """Tool-less structured-output agent; cached like get_settings (model config is static)."""
    return Agent(
        name="fact_extractor",
        instructions=EXTRACTION_INSTRUCTIONS,
        output_type=ExtractionResult,
        model=resolve_model(get_settings().triage_model),
        model_settings=ModelSettings(),
    )


async def extract_and_store(
    user_ref: str | None, message: str, session_id: str | None = None
) -> dict[str, int] | None:
    """Extract facts from one user message and merge them into user_facts.

    Existing facts ride along in the prompt so the model only returns new or contradicting
    facts (and reuses fact_type keys on updates); the deterministic merge rule in user_facts
    is still the last word. Never raises: memory is best-effort by contract.
    """
    if not user_ref or not message.strip():
        return None
    try:
        existing = get_user_facts(user_ref)
        existing_block = (
            "\n".join(f"- {f.fact_type}: {f.fact} (confidence {f.confidence})" for f in existing)
            or "(none)"
        )
        result = await Runner.run(
            _extractor(),
            f"Existing facts about this user:\n{existing_block}\n\nThe user's message:\n{message}",
            # M6 (ADR-043): own workflow name so this background call never blends into the
            # "chat" latency splits, but SAME session group — it is real conversation cost.
            run_config=RunConfig(
                workflow_name="memory-extraction",
                group_id=session_id,
                trace_metadata={"source": "internal", "user": user_ref},
            ),
        )
        candidates = [
            FactCandidate(fact_type=f.fact_type, fact=f.fact, confidence=f.confidence)
            for f in result.final_output.facts
        ]
        if not candidates:
            return {"inserted": 0, "updated": 0, "skipped": 0}
        source = f"extracted:{session_id}" if session_id else "extracted"
        counts = apply_extracted_facts(user_ref, candidates, source=source)
        logger.info("fact extraction for %s: %s", user_ref, counts)
        return counts
    except Exception:  # noqa: BLE001 — background task: log, never break the request path
        logger.exception("fact extraction failed (user=%s)", user_ref)
        return None

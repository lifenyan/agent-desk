"""POST /chat — load session + user facts, check semantic cache (read-only intents), then run the router agent."""
# Implemented in M1 (router run + structured citations); M2 added multi-turn continuity via
# session_id (ADR-019). M3 added the semantic-cache pre-check (ADR-023); M5 swapped the session
# backend to Postgres (ADR-030) and added the user-facts inject/extract hooks (ADR-031).
# M8 added the Slack source fields + the injection-guardrail tripwire contract (ADR-039/041).

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from agents import InputGuardrailTripwireTriggered, Runner
from agents.extensions.memory import SQLAlchemySession
from agents.tracing import custom_span, trace
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.context import ChatContext
from app.agents.router import router_agent
from app.cache import semantic_cache
from app.db.database import SessionLocal
from app.db.models import User
from app.memory import extraction, user_facts
from app.memory.session_store import get_session_store

router = APIRouter(tags=["chat"])


def _load_session(session_id: str | None) -> SQLAlchemySession | None:
    """Session handle for this conversation; None keeps the M1 one-shot behavior.

    This factory was the designed ADR-019 swap point: M2 shipped a file-backed SQLiteSession
    stopgap here; M5 replaced ONLY this body with the Postgres-backed store (ADR-030), so a
    conversation now survives API restarts and deploys."""
    if session_id is None:
        return None
    return get_session_store(session_id)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)  # 8000: Slack thread envelopes (M8)
    user_id: str | None = None  # trusted identity for tools (ChatContext) — never an LLM arg
    session_id: str | None = None  # client-generated; same id = same conversation (ADR-019)
    # M8 Slack ingestion (ADR-039). These fields come from the Socket Mode runner — a trusted
    # API client, same trust level as user_id (the M0 assumption: clients are authenticated
    # infrastructure, not end users). source="slack" arms the injection guardrail and turns
    # OFF the chat-only conveniences; the thread coordinates let post_slack_message reply.
    source: Literal["chat", "slack"] = "chat"
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    # Set by the runner on its ONE bounded re-submit after a guardrail trip (ADR-041):
    # the screen already fired and its finding is disclosed in the message preamble.
    injection_screened: bool = False


class Citation(BaseModel):
    article_id: str
    title: str
    rrf_score: float


class ChatResponse(BaseModel):
    answer: str
    agent: str  # which agent produced the final answer (router vs knowledge = routing visibility)
    citations: list[Citation]
    cached: bool = False  # True = served from the semantic cache, no agent ran (M3, ADR-023)
    # True = the M8 injection guardrail tripped and NO agent acted (ADR-041). The Slack runner
    # reacts by re-submitting once with injection_screened=True + a security preamble.
    flagged: bool = False


def _collect_citations(items: list[Any]) -> list[Citation]:
    """Structured citations = retrieved sources from the run's search-tool outputs.

    Honest framing: these are the articles put in front of the model (deduped, best RRF first),
    not a parse of which ones it chose to quote. Payloads with sufficient_evidence=false are
    skipped, so refusals return zero citations.
    """
    by_article: dict[str, Citation] = {}
    for item in items:
        if getattr(item, "type", None) != "tool_call_output_item":
            continue
        output = item.output
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                continue
        if not isinstance(output, dict):
            continue
        # get_release_notes nests one payload per version; search returns a single payload.
        payloads = [p for p in (output, *output.values()) if isinstance(p, dict) and "results" in p]
        for payload in payloads:
            if not payload.get("sufficient_evidence"):
                continue
            for r in payload["results"]:
                existing = by_article.get(r["article_id"])
                if existing is None or r["rrf_score"] > existing.rrf_score:
                    by_article[r["article_id"]] = Citation(
                        article_id=r["article_id"],
                        title=r["article_title"],
                        rrf_score=r["rrf_score"],
                    )
    return sorted(by_article.values(), key=lambda c: c.rrf_score, reverse=True)


async def _is_first_turn(session: SQLAlchemySession | None) -> bool:
    """Semantic-cache session policy (ADR-023): only a conversation's FIRST message may be
    cache-served or cache-stored. A mid-conversation message ("yes, go ahead", "what about
    v5.2?") means whatever the history makes it mean — matching it against a stored standalone
    Q&A is wrong even at similarity 1.0."""
    return session is None or not await session.get_items(limit=1)


def _trace_metadata(request: ChatRequest, *, cache_hit: bool) -> dict:
    """Trace tags/joins the Langfuse bridge aggregates on (M6, ADR-043). `source` keeps the
    M8 populations separable (Slack runs carry an extra screening call and skip the caches —
    blending them corrupts every latency/cost split); `input` is a preview, not the payload."""
    return {
        "source": request.source,
        "user": request.user_id or "",
        "cache_hit": "true" if cache_hit else "false",
        "input": request.message[:300],
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
    session = _load_session(request.session_id)
    first_turn = await _is_first_turn(session)

    # Long-term memory (ADR-031). Inject half: on a conversation's FIRST turn the acting
    # user's stored facts enter the session as ONE system item, so every later turn (and every
    # agent the router hands off to) sees them without re-reading the table. Extract half:
    # queued now, runs AFTER the response is sent (BackgroundTasks) on whichever branch below
    # returns — a slow or failed extraction can never delay or break the reply.
    # Chat-source only (ADR-039): a Slack envelope quotes OTHER people's messages — extracting
    # "user facts" from multi-author untrusted text would poison the acting user's memory.
    if request.source == "chat":
        if first_turn and session is not None and request.user_id:
            facts_item = await asyncio.to_thread(user_facts.injection_message, request.user_id)
            if facts_item is not None:
                await session.add_items([facts_item])
        background_tasks.add_task(
            extraction.extract_and_store, request.user_id, request.message, request.session_id
        )

    # M6 (ADR-043): ONE trace around everything user-facing in this turn — the cache lookup,
    # the agent run (Runner.run joins the ambient trace instead of opening its own), and the
    # cache store — so the trace's latency is the handler's real end-to-end time on BOTH the
    # hit and miss paths. A cache hit would otherwise emit no trace at all, silently deleting
    # the fast/cheap population from every latency/cost split. `metadata` is read by the
    # bridge at trace END, so flipping cache_hit after the lookup is safe by design.
    trace_meta = _trace_metadata(request, cache_hit=False)
    with trace("chat", group_id=request.session_id, metadata=trace_meta):
        # Semantic-cache pre-check (ADR-023): BEFORE any agent runs. Only read-only (knowledge)
        # answers are ever STORED, so a hit can never re-play an order or a ticket. to_thread:
        # the lookup does sync Redis + (on non-empty cache) one embedding call.
        # Chat-source only (ADR-039): a Slack report exists to become a ticket — serving it a
        # stored knowledge ANSWER (however similar the text) would silently drop the report.
        if first_turn and request.source == "chat":
            hit = await asyncio.to_thread(semantic_cache.lookup, request.message)
            if hit is not None:
                trace_meta["cache_hit"] = "true"
                with custom_span(
                    "semantic_cache_hit", data={"similarity": round(hit.similarity, 4)}
                ):
                    if session is not None:
                        # Keep the conversation coherent if the user keeps talking: the session
                        # must contain the turn we just short-circuited.
                        await session.add_items(
                            [
                                {"role": "user", "content": request.message},
                                {"role": "assistant", "content": hit.answer},
                            ]
                        )
                return ChatResponse(
                    answer=hit.answer,
                    agent="knowledge",  # entries are only ever written from knowledge runs
                    citations=[Citation(**c) for c in hit.citations],  # stored, NOT re-collected
                    cached=True,
                )

        try:
            result = await Runner.run(
                router_agent,
                request.message,
                context=ChatContext(
                    user_id=request.user_id,
                    source=request.source,
                    slack_channel=request.slack_channel,
                    slack_thread_ts=request.slack_thread_ts,
                    injection_screened=request.injection_screened,
                ),
                session=session,
            )
        except InputGuardrailTripwireTriggered as exc:
            # M8 injection screen (ADR-041): the run was halted BEFORE any agent acted. Not an
            # HTTP error — the flag is a first-class outcome the Slack runner reacts to (one
            # re-submit with injection_screened=True and a security preamble). The bridge tags
            # the trace flagged:true from the guardrail span itself.
            verdict = getattr(exc.guardrail_result.output, "output_info", None)
            evidence = getattr(verdict, "evidence", "") or ""
            return ChatResponse(
                answer=(
                    "Input flagged by the injection screen; no agent ran. "
                    f"Evidence: {evidence or 'n/a'}"
                ),
                agent="guardrail",
                citations=[],
                flagged=True,
            )
        answer = str(result.final_output)
        citations = _collect_citations(result.new_items)

        # Write side of the read-only guarantee: knowledge answers with evidence only (never
        # fulfillment/incident, never refusals) — and only first-turn ones, symmetric with
        # lookup. Source-gated like the lookup (M8): a misrouted Slack envelope answered by
        # the knowledge agent must not become a cache entry keyed on envelope text.
        citation_dicts = [c.model_dump() for c in citations]
        if (
            first_turn
            and request.source == "chat"
            and semantic_cache.is_cacheable(result.last_agent.name, answer, citation_dicts)
        ):
            await asyncio.to_thread(semantic_cache.store, request.message, answer, citation_dicts)

        return ChatResponse(answer=answer, agent=result.last_agent.name, citations=citations)


class IdentityResolveResponse(BaseModel):
    found: bool
    name: str | None = None  # display name for the runner's replies; never an id


@router.get("/identity/resolve", response_model=IdentityResolveResponse)
async def resolve_identity(email: str) -> IdentityResolveResponse:
    """Does this email map to a service-desk user? The Slack runner's fail-closed pre-check
    (ADR-039): an unmatched Slack profile never reaches the pipeline — the runner posts a
    deterministic fallback reply instead of letting an agent act with no resolvable identity."""

    def _lookup() -> User | None:
        with SessionLocal() as s:
            return s.scalar(select(User).where(User.email == email))

    user = await asyncio.to_thread(_lookup)
    return IdentityResolveResponse(found=user is not None, name=user.name if user else None)

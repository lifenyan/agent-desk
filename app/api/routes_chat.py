"""POST /chat + /chat/stream — load session + user facts, check semantic cache (read-only intents), then run the router agent."""
# Implemented in M1 (router run + structured citations); M2 added multi-turn continuity via
# session_id (ADR-019). M3 added the semantic-cache pre-check (ADR-023); M5 swapped the session
# backend to Postgres (ADR-030) and added the user-facts inject/extract hooks (ADR-031).
# M8 added the Slack source fields + the injection-guardrail tripwire contract (ADR-039/041).
# M11 added POST /chat/stream (SSE, ADR-048) and extracted the shared pipeline helpers so the
# two endpoints cannot drift — the /chat JSON contract itself is FROZEN (its clients: the e2e
# suite, the Slack runner, the eval harness, MCP-adjacent surfaces).

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from agents import InputGuardrailTripwireTriggered, Runner
from agents.extensions.memory import SQLAlchemySession
from agents.stream_events import StreamEvent
from agents.tracing import custom_span, trace
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.agents.context import ChatContext
from app.agents.router import router_agent
from app.cache import semantic_cache
from app.db.database import SessionLocal
from app.db.models import User
from app.memory import extraction, user_facts
from app.memory.session_store import get_session_store

logger = logging.getLogger(__name__)

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


def _confirm_ticket_actions(answer: str, items: list[Any]) -> str:
    """Deterministic backstop for the model narrating instead of confirming (observed live:
    "I'll open a ticket now…" as the final text while create_ticket had ALREADY succeeded).
    Every ticket number a write tool returned this run must appear in the final answer —
    append the ones the model dropped. Numbers come from tool payloads, never model prose,
    so this can never confirm an action that didn't happen (the ADR-041 honesty rule's
    deterministic other half)."""
    numbers: list[str] = []
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
        ticket = output.get("ticket")
        # create_ticket/update_ticket payloads carry "number"; get_ticket_status (a READ —
        # nothing to confirm) is excluded by its comment fields.
        if isinstance(ticket, dict) and ticket.get("number") and "latest_comment" not in ticket:
            numbers.append(ticket["number"])
        comment = output.get("comment")
        if isinstance(comment, dict) and comment.get("ticket_number"):
            numbers.append(comment["ticket_number"])
    missing = [n for n in dict.fromkeys(numbers) if n not in answer]
    if missing:
        answer = answer.rstrip() + "\n\n🎫 " + " · ".join(f"Ticket {n}" for n in missing)
    return answer


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


# --- the shared pipeline (M11) ----------------------------------------------------------------
# /chat (frozen JSON contract) and /chat/stream (SSE, ADR-048) are two presentations of ONE
# pipeline: every invariant-dense step lives in a helper below so the endpoints cannot drift —
# facts injection (ADR-031), the semantic-cache consult/store pair (ADR-023), the guardrail-trip
# contract (ADR-041), citation collection + the ticket-number backstop. Extracting these did NOT
# change /chat's semantics: same steps, same order, same conditions (the untouched 18-flow e2e
# suite is the proof).


async def _inject_user_facts(
    request: ChatRequest, session: SQLAlchemySession | None, first_turn: bool
) -> None:
    """Long-term memory, inject half (ADR-031): on a conversation's FIRST turn the acting
    user's stored facts enter the session as ONE system item, so every later turn (and every
    agent the router hands off to) sees them without re-reading the table. Callers gate on
    source=="chat" (ADR-039): a Slack envelope quotes OTHER people's messages — extracting or
    injecting "user facts" around multi-author untrusted text would poison the acting user's
    memory."""
    if first_turn and session is not None and request.user_id:
        facts_item = await asyncio.to_thread(user_facts.injection_message, request.user_id)
        if facts_item is not None:
            await session.add_items([facts_item])


async def _serve_cache_hit(
    request: ChatRequest,
    session: SQLAlchemySession | None,
    first_turn: bool,
    trace_meta: dict,
) -> ChatResponse | None:
    """Semantic-cache pre-check (ADR-023): BEFORE any agent runs. Only read-only (knowledge)
    answers are ever STORED, so a hit can never re-play an order or a ticket. to_thread:
    the lookup does sync Redis + (on non-empty cache) one embedding call.
    Chat-source only (ADR-039): a Slack report exists to become a ticket — serving it a
    stored knowledge ANSWER (however similar the text) would silently drop the report."""
    if not (first_turn and request.source == "chat"):
        return None
    hit = await asyncio.to_thread(semantic_cache.lookup, request.message)
    if hit is None:
        return None
    trace_meta["cache_hit"] = "true"
    with custom_span("semantic_cache_hit", data={"similarity": round(hit.similarity, 4)}):
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


def _run_context(request: ChatRequest) -> ChatContext:
    """Trusted per-run state for tools — identity and Slack coordinates are never LLM args."""
    return ChatContext(
        user_id=request.user_id,
        source=request.source,
        slack_channel=request.slack_channel,
        slack_thread_ts=request.slack_thread_ts,
        injection_screened=request.injection_screened,
    )


def _flagged_response(exc: InputGuardrailTripwireTriggered) -> ChatResponse:
    """M8 injection screen (ADR-041): the run was halted BEFORE any agent acted. Not an
    HTTP error — the flag is a first-class outcome the Slack runner reacts to (one
    re-submit with injection_screened=True and a security preamble). The bridge tags
    the trace flagged:true from the guardrail span itself."""
    verdict = getattr(exc.guardrail_result.output, "output_info", None)
    evidence = getattr(verdict, "evidence", "") or ""
    return ChatResponse(
        answer=(
            f"Input flagged by the injection screen; no agent ran. Evidence: {evidence or 'n/a'}"
        ),
        agent="guardrail",
        citations=[],
        flagged=True,
    )


async def _finalize_run(request: ChatRequest, result: Any, first_turn: bool) -> ChatResponse:
    """Post-run half shared by both endpoints (result: RunResult or RunResultStreaming —
    same fields). Ticket-number backstop, citation collection, then the write side of the
    read-only cache guarantee: knowledge answers with evidence only (never fulfillment/
    incident, never refusals) — and only first-turn ones, symmetric with lookup. Source-gated
    like the lookup (M8): a misrouted Slack envelope answered by the knowledge agent must not
    become a cache entry keyed on envelope text. For a streamed run this executes only after
    the full answer is assembled — the store is never fed a partial answer."""
    answer = _confirm_ticket_actions(str(result.final_output), result.new_items)
    citations = _collect_citations(result.new_items)
    citation_dicts = [c.model_dump() for c in citations]
    if (
        first_turn
        and request.source == "chat"
        and semantic_cache.is_cacheable(result.last_agent.name, answer, citation_dicts)
    ):
        await asyncio.to_thread(semantic_cache.store, request.message, answer, citation_dicts)
    return ChatResponse(answer=answer, agent=result.last_agent.name, citations=citations)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
    session = _load_session(request.session_id)
    first_turn = await _is_first_turn(session)

    # Long-term memory (ADR-031): inject on the first turn; extraction queued now, runs AFTER
    # the response is sent (BackgroundTasks) on whichever branch below returns — a slow or
    # failed extraction can never delay or break the reply. Chat-source only (ADR-039).
    if request.source == "chat":
        await _inject_user_facts(request, session, first_turn)
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
        cached = await _serve_cache_hit(request, session, first_turn, trace_meta)
        if cached is not None:
            return cached

        try:
            result = await Runner.run(
                router_agent,
                request.message,
                context=_run_context(request),
                session=session,
            )
        except InputGuardrailTripwireTriggered as exc:
            return _flagged_response(exc)

        return await _finalize_run(request, result, first_turn)


# --- POST /chat/stream (M11, ADR-048) ----------------------------------------------------------

# Run-item events worth relaying as `status` frames: cheap perceived-latency value during the
# pre-text seconds (router classification + tool calls happen before the first visible token).
_STATUS_EVENT_NAMES = {"handoff_occured", "tool_called"}  # "occured": SDK's own (frozen) typo


def _sse(event: str, data: dict) -> str:
    """One SSE frame. json.dumps emits no raw newlines, so a single data: line is always valid."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _status_payload(event: StreamEvent) -> dict | None:
    """Which specialist took over / which tool is running — from run-item events, never prose.
    None = drop the frame: handoffs surface as tool_called("transfer_to_X") first (the SDK
    models them as tool calls), and the handoff_occured frame right behind it is the real
    signal — relaying both would show a phantom "running transfer_to_knowledge" status."""
    if event.name == "handoff_occured":
        target = getattr(getattr(event.item, "target_agent", None), "name", None)
        return {"stage": "handoff", "detail": target or "specialist"}
    tool_name = getattr(event.item.raw_item, "name", "tool")
    if tool_name.startswith("transfer_to_"):
        return None
    return {"stage": "tool", "detail": tool_name}


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """SSE twin of POST /chat — same ChatRequest in, `text/event-stream` out (ADR-048).

    Events: `delta` (text tokens as the specialist generates), `status` (handoff/tool
    progress), `final` (the full ChatResponse-equivalent JSON — built from ChatResponse
    itself, so the two payloads cannot drift), `error` (mid-stream failure; HTTP status is
    already committed at 200 by then, so the error travels in-band). A semantic-cache hit
    emits the stored answer as ONE delta + final immediately — no fake token-drip.

    Consumed (for now) only by the Streamlit chat UI; every other client stays on the frozen
    /chat JSON contract (ADR-038's pure-client pattern extended to the browser)."""
    session = _load_session(request.session_id)
    first_turn = await _is_first_turn(session)

    async def stream() -> AsyncIterator[str]:
        # Same ONE-trace rule as /chat (ADR-043), spanning the WHOLE stream: the trace opens
        # before the cache lookup and closes when the generator finishes (final/error emitted),
        # so its latency is the real end-to-end streaming time — TTFT wins must show up as
        # perceived latency, never as shortened traces.
        trace_meta = _trace_metadata(request, cache_hit=False)
        with trace("chat", group_id=request.session_id, metadata=trace_meta):
            try:
                if request.source == "chat":
                    await _inject_user_facts(request, session, first_turn)
                cached = await _serve_cache_hit(request, session, first_turn, trace_meta)
                if cached is not None:
                    yield _sse("delta", {"text": cached.answer})
                    yield _sse("final", cached.model_dump())
                    return

                result = Runner.run_streamed(
                    router_agent,
                    request.message,
                    context=_run_context(request),
                    session=session,
                )
                try:
                    async for event in result.stream_events():
                        if event.type == "raw_response_event":
                            if getattr(event.data, "type", "") == "response.output_text.delta":
                                yield _sse("delta", {"text": event.data.delta})
                        elif (
                            event.type == "run_item_stream_event"
                            and event.name in _STATUS_EVENT_NAMES
                        ):
                            status = _status_payload(event)
                            if status is not None:
                                yield _sse("status", status)
                except InputGuardrailTripwireTriggered as exc:
                    yield _sse("final", _flagged_response(exc).model_dump())
                    return

                final = await _finalize_run(request, result, first_turn)
                yield _sse("final", final.model_dump())
            except Exception as exc:  # noqa: BLE001 — status already committed at 200; the
                # in-band `error` frame is the only channel left (ADR-048). This handler adds
                # nothing partial to the session on this path — the SDK's own persistence is
                # the same code for streamed and blocking runs, so failure behavior matches
                # /chat exactly.
                logger.exception("chat stream failed mid-flight")
                yield _sse("error", {"detail": f"{exc.__class__.__name__}: {exc}"})

    # ADR-031 under streaming: FastAPI's BackgroundTasks fires after a NORMAL response only —
    # for a StreamingResponse the task must ride the response object itself. Starlette runs it
    # after the stream closes (pinned by test), so extraction can never delay a token, and it
    # runs on the error path too (same as /chat). Chat-source only (ADR-039).
    background = None
    if request.source == "chat":
        background = BackgroundTask(
            extraction.extract_and_store, request.user_id, request.message, request.session_id
        )
    return StreamingResponse(stream(), media_type="text/event-stream", background=background)


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

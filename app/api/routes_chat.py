"""POST /chat — load session + user facts, check semantic cache (read-only intents), then run the router agent."""
# Implemented in M1 (router run + structured citations); M2 added multi-turn continuity via
# session_id (ADR-019). M3 added the semantic-cache pre-check (ADR-023); M5 swapped the session
# backend to Postgres (ADR-030) and added the user-facts inject/extract hooks (ADR-031).

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import Runner
from agents.extensions.memory import SQLAlchemySession
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from app.agents.context import ChatContext
from app.agents.router import router_agent
from app.cache import semantic_cache
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
    message: str = Field(min_length=1, max_length=4000)
    user_id: str | None = None  # trusted identity for tools (ChatContext) — never an LLM arg
    session_id: str | None = None  # client-generated; same id = same conversation (ADR-019)


class Citation(BaseModel):
    article_id: str
    title: str
    rrf_score: float


class ChatResponse(BaseModel):
    answer: str
    agent: str  # which agent produced the final answer (router vs knowledge = routing visibility)
    citations: list[Citation]
    cached: bool = False  # True = served from the semantic cache, no agent ran (M3, ADR-023)


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


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
    session = _load_session(request.session_id)
    first_turn = await _is_first_turn(session)

    # Long-term memory (ADR-031). Inject half: on a conversation's FIRST turn the acting
    # user's stored facts enter the session as ONE system item, so every later turn (and every
    # agent the router hands off to) sees them without re-reading the table. Extract half:
    # queued now, runs AFTER the response is sent (BackgroundTasks) on whichever branch below
    # returns — a slow or failed extraction can never delay or break the reply.
    if first_turn and session is not None and request.user_id:
        facts_item = await asyncio.to_thread(user_facts.injection_message, request.user_id)
        if facts_item is not None:
            await session.add_items([facts_item])
    background_tasks.add_task(
        extraction.extract_and_store, request.user_id, request.message, request.session_id
    )

    # Semantic-cache pre-check (ADR-023): BEFORE any agent runs. Only read-only (knowledge)
    # answers are ever STORED, so a hit can never re-play an order or a ticket. to_thread:
    # the lookup does sync Redis + (on non-empty cache) one embedding call.
    if first_turn:
        hit = await asyncio.to_thread(semantic_cache.lookup, request.message)
        if hit is not None:
            if session is not None:
                # Keep the conversation coherent if the user keeps talking: the session must
                # contain the turn we just short-circuited.
                await session.add_items(
                    [
                        {"role": "user", "content": request.message},
                        {"role": "assistant", "content": hit.answer},
                    ]
                )
            return ChatResponse(
                answer=hit.answer,
                agent="knowledge",  # entries are only ever written from knowledge runs
                citations=[Citation(**c) for c in hit.citations],  # stored; NOT _collect_citations
                cached=True,
            )

    result = await Runner.run(
        router_agent,
        request.message,
        context=ChatContext(user_id=request.user_id),
        session=session,
    )
    answer = str(result.final_output)
    citations = _collect_citations(result.new_items)

    # Write side of the read-only guarantee: knowledge answers with evidence only (never
    # fulfillment/incident, never refusals) — and only first-turn ones, symmetric with lookup.
    citation_dicts = [c.model_dump() for c in citations]
    if first_turn and semantic_cache.is_cacheable(result.last_agent.name, answer, citation_dicts):
        await asyncio.to_thread(semantic_cache.store, request.message, answer, citation_dicts)

    return ChatResponse(answer=answer, agent=result.last_agent.name, citations=citations)

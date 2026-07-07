"""POST /chat — load session + user facts, check semantic cache (read-only intents), then run the router agent."""
# Implemented in M1 (router run + structured citations); M2 added multi-turn continuity via
# session_id (ADR-019). M3 adds the semantic-cache pre-check; M5 adds the Postgres session
# backend + user-facts loading.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents import Runner, SQLiteSession
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.agents.context import ChatContext
from app.agents.router import router_agent

router = APIRouter(tags=["chat"])

# Pre-M5 session stopgap (ADR-019): file-backed SQLite under the git-ignored scratch dir, so
# the fulfillment confirm/HITL dialogue can span turns TODAY. This factory is the single swap
# point — M5 replaces ONLY its body with the Postgres-backed store (app/memory/session_store.py)
# and nothing else changes.
_SESSION_DB = Path(__file__).resolve().parents[2] / "ignore" / "chat_sessions.sqlite3"


def _load_session(session_id: str | None) -> SQLiteSession | None:
    """Session handle for this conversation; None keeps the M1 one-shot behavior."""
    if session_id is None:
        return None
    _SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteSession(session_id, _SESSION_DB)


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


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    result = await Runner.run(
        router_agent,
        request.message,
        context=ChatContext(user_id=request.user_id),
        session=_load_session(request.session_id),
    )
    return ChatResponse(
        answer=str(result.final_output),
        agent=result.last_agent.name,
        citations=_collect_citations(result.new_items),
    )

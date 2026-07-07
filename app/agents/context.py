"""Per-run context object passed to agents and tools: user_id, injected user facts, session handle."""
# Implemented in M1 (user_id only). M5 adds facts injected from long-term memory + session handle.

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChatContext:
    """Local-only run state (never sent to the LLM — that's what instructions/messages are for)."""

    user_id: str | None = None
    # TODO(M5): user_facts: list[str], session handle (SDK session persisted in Postgres)

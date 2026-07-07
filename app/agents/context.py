"""Per-run context object passed to agents and tools: the trusted acting-user identity."""
# Implemented in M1 (user_id only). M5 deliberately did NOT add fields here: user facts are
# injected as a session item at session start (ADR-031 — persisted with the conversation, seen
# by every agent, no per-turn re-read), and the session handle is passed to Runner.run directly
# by routes_chat — neither is per-tool state, which is all this context is for.

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChatContext:
    """Local-only run state (never sent to the LLM — that's what instructions/messages are for)."""

    user_id: str | None = None

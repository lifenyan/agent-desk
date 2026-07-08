"""Per-run context object passed to agents and tools: the trusted acting-user identity."""
# Implemented in M1 (user_id only). M5 deliberately did NOT add fields here: user facts are
# injected as a session item at session start (ADR-031 — persisted with the conversation, seen
# by every agent, no per-turn re-read), and the session handle is passed to Runner.run directly
# by routes_chat — neither is per-tool state, which is all this context is for.
# M8 added the Slack fields: where a run may post (channel/thread) is trusted per-run state the
# LLM must never supply — the same argument-trust layer as user_id (ADR-039).

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChatContext:
    """Local-only run state (never sent to the LLM — that's what instructions/messages are for)."""

    user_id: str | None = None
    # Where this run came from: "chat" (Streamlit/API clients), "slack" (the Socket Mode
    # runner), or "mcp" (mcp_server building contexts for the plain tools). Gates the M8
    # injection guardrail and the chat-only conveniences (semantic cache, memory hooks).
    source: str = "chat"
    # Slack thread coordinates, set by routes_chat from the (trusted) runner request — NEVER
    # LLM arguments: post_slack_message reads them here, so a prompt-injected model cannot
    # post to a channel of its choosing (the user_tools DESIGN NOTE, applied to Slack).
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    # True on the ONE bounded re-run after the injection guardrail tripped (ADR-041): the
    # screen already fired, its finding is in the message preamble, so it must not re-trip.
    injection_screened: bool = False

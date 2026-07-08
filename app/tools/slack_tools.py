"""Slack tools: post_slack_message — post the agent's reply into the triggering Slack thread.

House pattern (ADR-004 discipline, user_tools DESIGN NOTE): a plain function first, then the
@function_tool wrapper. The DESTINATION (channel + thread_ts) comes from ChatContext — set by
routes_chat from the trusted Slack-runner request, never an LLM argument — so a prompt-injected
model can compose the message text but can never choose where it goes (ADR-039).

Degradation contract (M8): CI and local dev run without Slack. Missing credentials, a missing
thread context, or a Slack API failure all return a logged error dict (the SDK feeds it back
and the agent can still end its turn) — never an exception. When settings.slack_sink_file is
set, messages are appended there as JSON lines instead of hitting Slack: the test seam the
slack eval suite reads to assert reply content with no live workspace.
"""
# Implemented in M8.

from __future__ import annotations

import json
import logging

from agents import RunContextWrapper, function_tool

from app.agents.context import ChatContext
from app.config import get_settings

logger = logging.getLogger(__name__)


def post_slack_message(ctx: RunContextWrapper[ChatContext], text: str) -> dict:
    """Post a message into the Slack thread this conversation came from. Only works for
    conversations ingested from Slack — the thread is fixed, you cannot choose a channel.

    Args:
        text: The message to post. Include the ticket id you created or linked, and one
            relevant knowledge-base article title if you found one.
    """
    context = ctx.context
    channel = context.slack_channel if context is not None else None
    thread_ts = context.slack_thread_ts if context is not None else None
    if not channel or not thread_ts:
        return {
            "error": "not a Slack conversation: there is no thread to post to. "
            "Just answer the user directly in chat."
        }

    settings = get_settings()
    if settings.slack_sink_file:
        # Test seam (ADR-039): capture instead of send. Evals assert on this file.
        with open(settings.slack_sink_file, "a") as f:
            f.write(json.dumps({"channel": channel, "thread_ts": thread_ts, "text": text}) + "\n")
        return {"slack_message": {"channel": channel, "thread_ts": thread_ts, "posted": True}}

    if not settings.slack_bot_token:
        # Graceful no-op: Slack-less environments must never crash an agent run.
        logger.warning("post_slack_message: SLACK_BOT_TOKEN not configured; message not sent")
        return {
            "error": "Slack credentials are not configured; the message was not sent. "
            "Give the user the same information in your final answer instead."
        }

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        WebClient(token=settings.slack_bot_token).chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text
        )
    except SlackApiError as exc:
        logger.warning("post_slack_message failed: %s", exc)
        return {"error": f"Slack rejected the message ({exc.response['error']}); not sent"}
    return {"slack_message": {"channel": channel, "thread_ts": thread_ts, "posted": True}}


# --- Agents SDK wrapper (schema derived from the signature + docstring above) ---
post_slack_message_tool = function_tool(post_slack_message)

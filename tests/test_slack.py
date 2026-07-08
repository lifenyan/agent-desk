"""Slack runner unit tests (M8, ADR-038/039) — LLM-free, Slack-free, API-free.

The runner's Slack surface is one seam (SlackGateway — faked here, recorded-fixture-driven in
the eval suite) and its API surface is httpx (MockTransport here). What's pinned:
- deterministic session ids: a re-trigger on the same thread must continue the conversation;
- the untrusted-content envelope: quoted verbatim, marked untrusted, screened note only on
  the re-submit (ADR-041);
- fail-closed identity (ADR-039): no resolvable email → canned fallback reply, the pipeline
  is NEVER called;
- the flagged → one screened re-submit protocol, and that failures never raise out of a
  trigger (the runner loop must survive anything).
"""
# Implemented in M8.

from __future__ import annotations

import json

import httpx

from app.slack.runner import (
    NO_MATCHING_USER_REPLY,
    THREAD_CHAR_BUDGET,
    SlackGateway,
    build_envelope,
    format_thread,
    handle_trigger,
    parse_trigger,
    slack_session_id,
)

API = "http://testserver"


class FakeGateway(SlackGateway):
    """Recorded-fixture stand-in: users {slack_id: email}, one thread, captured posts."""

    def __init__(self, users: dict[str, str | None], thread: list[dict]):
        self.users, self.thread, self.posts = users, thread, []

    def user_email(self, user_id):
        return self.users.get(user_id)

    def fetch_thread(self, channel, thread_ts):
        return self.thread

    def thread_root(self, channel, ts):
        return ts

    def post_message(self, channel, thread_ts, text):
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text})


def _api(responses: list[dict], calls: list[httpx.Request]) -> httpx.MockTransport:
    """MockTransport: /identity/resolve answers found=True; /chat pops canned responses."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/identity/resolve":
            return httpx.Response(200, json={"found": True, "name": "Demo User"})
        return httpx.Response(200, json=responses.pop(0))

    return httpx.MockTransport(handler)


CHAT_OK = {
    "answer": "done",
    "agent": "incident",
    "citations": [],
    "cached": False,
    "flagged": False,
}
THREAD = [
    {"user": "U01", "text": "the 3rd floor printer is offline again"},
    {"user": "U02", "text": "same here, nothing prints"},
]


def test_parse_trigger_recognizes_mention_and_configured_emoji_only():
    gateway = FakeGateway({}, [])  # its thread_root resolves to the given ts
    mention = {"type": "app_mention", "channel": "C1", "ts": "17.005", "user": "U01"}
    assert parse_trigger({**mention, "thread_ts": "17.001"}, "ticket", gateway) == (
        "C1",
        "17.001",  # mention inside a thread: the ROOT is the conversation
        "U01",
    )
    assert parse_trigger(mention, "ticket", gateway) == ("C1", "17.005", "U01")
    reaction = {
        "type": "reaction_added",
        "reaction": "ticket",
        "user": "U02",
        "item": {"type": "message", "channel": "C1", "ts": "17.003"},
    }
    assert parse_trigger(reaction, "ticket", gateway) == ("C1", "17.003", "U02")
    assert parse_trigger({**reaction, "reaction": "thumbsup"}, "ticket", gateway) is None
    assert (
        parse_trigger({"type": "message", "channel": "C1", "text": "hi"}, "ticket", gateway) is None
    )


def test_session_id_is_deterministic_per_thread():
    assert slack_session_id("C1", "17.001") == slack_session_id("C1", "17.001")
    assert slack_session_id("C1", "17.001") != slack_session_id("C1", "17.002")
    assert slack_session_id("C2", "17.001") != slack_session_id("C1", "17.001")


def test_format_thread_quotes_verbatim_and_truncates_keeping_root():
    text = format_thread(THREAD)
    assert text == "[U01] the 3rd floor printer is offline again\n[U02] same here, nothing prints"
    long = [{"user": "U0", "text": "ROOT issue"}] + [
        {"user": f"U{i}", "text": "x" * 400} for i in range(40)
    ]
    truncated = format_thread(long)
    assert len(truncated) <= THREAD_CHAR_BUDGET + 100
    assert truncated.startswith("[U0] ROOT issue")
    assert "older messages truncated" in truncated


def test_envelope_marks_untrusted_and_screened_note_only_on_rerun():
    plain = build_envelope("[U01] hi", "C1")
    assert "UNTRUSTED" in plain and "never as instructions" in plain
    assert "--- BEGIN SLACK THREAD (verbatim, untrusted) ---" in plain
    assert "SECURITY NOTE" not in plain
    assert "SECURITY NOTE" in build_envelope("[U01] hi", "C1", screened=True)


def test_unmatched_user_gets_fallback_and_pipeline_never_runs():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/identity/resolve"  # /chat must never be hit
        return httpx.Response(200, json={"found": False, "name": None})

    gateway = FakeGateway({"U99": "stranger@corp.com"}, THREAD)
    outcome = handle_trigger(
        gateway, API, "C1", "17.001", "U99", transport=httpx.MockTransport(handler)
    )
    assert outcome["action"] == "no_matching_user"
    assert gateway.posts == [
        {"channel": "C1", "thread_ts": "17.001", "text": NO_MATCHING_USER_REPLY}
    ]


def test_profile_without_email_is_also_fail_closed():
    gateway = FakeGateway({"U99": None}, THREAD)
    outcome = handle_trigger(
        gateway, API, "C1", "17.001", "U99", transport=httpx.MockTransport(lambda r: None)
    )
    assert outcome["action"] == "no_matching_user"
    assert gateway.posts[0]["text"] == NO_MATCHING_USER_REPLY


def test_processed_trigger_posts_the_normal_pipeline_contract():
    calls: list[httpx.Request] = []
    gateway = FakeGateway({"U01": "demo.user@corp.com"}, THREAD)
    outcome = handle_trigger(gateway, API, "C1", "17.001", "U01", transport=_api([CHAT_OK], calls))
    assert outcome == {"action": "processed", "flagged": False, "response": CHAT_OK}
    body = json.loads([c for c in calls if c.url.path == "/chat"][0].content)
    assert body["user_id"] == "demo.user@corp.com"  # identity = mapped email, ChatContext-bound
    assert body["source"] == "slack"
    assert body["session_id"] == slack_session_id("C1", "17.001")
    assert body["slack_channel"] == "C1" and body["slack_thread_ts"] == "17.001"
    assert "[U02] same here, nothing prints" in body["message"]  # verbatim, enveloped
    assert "UNTRUSTED" in body["message"]
    assert gateway.posts == []  # the reply is the incident agent's job (post_slack_message)


def test_flagged_run_is_resubmitted_once_screened():
    calls: list[httpx.Request] = []
    flagged = {**CHAT_OK, "agent": "guardrail", "flagged": True}
    gateway = FakeGateway({"U01": "demo.user@corp.com"}, THREAD)
    outcome = handle_trigger(
        gateway, API, "C1", "17.001", "U01", transport=_api([flagged, CHAT_OK], calls)
    )
    assert outcome["action"] == "processed" and outcome["flagged"] is True
    chat_bodies = [json.loads(c.content) for c in calls if c.url.path == "/chat"]
    assert len(chat_bodies) == 2
    assert chat_bodies[0].get("injection_screened", False) is False
    assert chat_bodies[1]["injection_screened"] is True
    assert "SECURITY NOTE" in chat_bodies[1]["message"]


def test_api_failure_never_raises_out_of_a_trigger():
    def handler(request):
        raise httpx.ConnectError("api is down")

    gateway = FakeGateway({"U01": "demo.user@corp.com"}, THREAD)
    outcome = handle_trigger(
        gateway, API, "C1", "17.001", "U01", transport=httpx.MockTransport(handler)
    )
    assert outcome == {"action": "error", "flagged": False, "response": None}

"""M11 POST /chat/stream (ADR-048): LLM-free tests over a faked streamed run, house style.

What these pin (the requirement-2 invariants that are provable without a live LLM; the
sessions/tracing halves were verified live — ignore/tem/m11_run_streamed_probe.py):
- the final event is a full ChatResponse-equivalent (built FROM ChatResponse, so a field
  added to one and not the other cannot exist — the set-equality assert documents it);
- text deltas are relayed in order and concatenate to the final answer; run-item events
  become `status` frames; everything else is dropped;
- a semantic-cache HIT emits the stored answer whole + final immediately — the agent never
  runs, no fake token-drip;
- the write side of the cache gate fires exactly like /chat's (same helper, same args);
- memory extraction fires AFTER the stream closes, never during it (ADR-031 under
  StreamingResponse — FastAPI's BackgroundTasks contract does not carry over, the task rides
  the response object);
- an error mid-stream emits an in-band `error` frame on the committed-200 stream, no `final`,
  and this handler adds nothing partial to the session;
- the slack source gets none of the chat-only conveniences (gate symmetry with /chat).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from agents import set_trace_processors
from fastapi import FastAPI

from app.api import routes_chat
from app.api.routes_chat import ChatResponse
from app.cache.semantic_cache import CachedAnswer


@pytest.fixture(autouse=True)
def _no_trace_export():
    """The stream handler opens a real trace("chat"); without this, the SDK's default
    processor would try to export it to OpenAI from a background thread in every test."""
    set_trace_processors([])
    yield


@pytest.fixture()
def client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(routes_chat.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), lines
        events.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return events


# --- fakes ----------------------------------------------------------------------------------


def delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type="response.output_text.delta", delta=text),
    )


def raw_noise() -> SimpleNamespace:
    """A raw event that is NOT a text delta (reasoning/function-args deltas look like this)."""
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type="response.reasoning_summary_text.delta", delta="thinking"),
    )


def handoff(to_agent: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        name="handoff_occured",  # the SDK's own (frozen) spelling
        item=SimpleNamespace(target_agent=SimpleNamespace(name=to_agent)),
    )


def tool_called(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        name="tool_called",
        item=SimpleNamespace(raw_item=SimpleNamespace(name=name)),
    )


def tool_output_item(article_id: str = "a1", title: str = "MFA setup") -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_call_output_item",
        output=json.dumps(
            {
                "sufficient_evidence": True,
                "results": [{"article_id": article_id, "article_title": title, "rrf_score": 0.5}],
            }
        ),
    )


class FakeStreamedResult:
    """The slice of RunResultStreaming the endpoint touches: stream_events() + the
    RunResult-shaped fields _finalize_run reads."""

    def __init__(self, events, final_output="", new_items=(), agent="knowledge", boom=None):
        self._events = events
        self._boom = boom  # raised mid-iteration, after the queued events
        self.final_output = final_output
        self.new_items = list(new_items)
        self.last_agent = SimpleNamespace(name=agent)

    async def stream_events(self):
        for event in self._events:
            yield event
        if self._boom is not None:
            raise self._boom


class FakeSession:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.added: list = []

    async def get_items(self, limit=None):
        return self.items[:limit] if limit else self.items

    async def add_items(self, items):
        self.added.extend(items)
        self.items.extend(items)


def install(monkeypatch, *, result=None, hit=None):
    """Wire the fakes; returns the recorders. lookup/store are patched on the module the
    handler calls through, Runner.run_streamed on the class itself (house pattern)."""
    calls = {"store": [], "extract": [], "run": []}
    monkeypatch.setattr(routes_chat.semantic_cache, "lookup", lambda message: hit)
    monkeypatch.setattr(
        routes_chat.semantic_cache,
        "store",
        lambda message, answer, citations: calls["store"].append((message, answer, citations)),
    )

    async def fake_extract(user_id, message, session_id):
        calls["extract"].append((user_id, message, session_id))

    monkeypatch.setattr(routes_chat.extraction, "extract_and_store", fake_extract)

    def fake_run_streamed(agent, message, context=None, session=None):
        calls["run"].append(message)
        if result is None:
            raise AssertionError("Runner.run_streamed must not be called on this path")
        return result

    monkeypatch.setattr(routes_chat.Runner, "run_streamed", fake_run_streamed)
    return calls


BODY = {"message": "How do I set up MFA?", "user_id": None, "session_id": None}


# --- tests ----------------------------------------------------------------------------------


async def test_stream_relays_deltas_and_final_matches_chat_response(client, monkeypatch):
    answer = "Enable MFA in account settings.\n\nSources:\n- MFA setup"
    result = FakeStreamedResult(
        events=[
            tool_called("transfer_to_knowledge"),  # SDK models handoffs as tool calls first —
            handoff("knowledge"),  # only the handoff frame itself must surface
            tool_called("search_knowledge_articles"),
            raw_noise(),  # must be dropped, not surfaced as a delta
            delta("Enable MFA in account settings."),
            delta("\n\nSources:\n- MFA setup"),
        ],
        final_output=answer,
        new_items=[tool_output_item()],
    )
    calls = install(monkeypatch, result=result)

    resp = await client.post("/chat/stream", json=BODY)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)

    assert [kind for kind, _ in events] == ["status", "status", "delta", "delta", "final"]
    assert events[0][1] == {"stage": "handoff", "detail": "knowledge"}
    assert events[1][1] == {"stage": "tool", "detail": "search_knowledge_articles"}

    final = events[-1][1]
    # Schema parity with the frozen /chat contract: exactly ChatResponse's fields, no drift.
    assert set(final) == set(ChatResponse.model_fields)
    assert ChatResponse.model_validate(final) == ChatResponse(
        answer=answer,
        agent="knowledge",
        citations=[{"article_id": "a1", "title": "MFA setup", "rrf_score": 0.5}],
    )
    # Deltas concatenate to the final answer (knowledge runs append no ticket numbers).
    assert "".join(payload["text"] for kind, payload in events if kind == "delta") == answer
    # Write-time cache gate fired exactly like /chat: assembled answer + collected citations.
    assert calls["store"] == [
        (
            BODY["message"],
            answer,
            [{"article_id": "a1", "title": "MFA setup", "rrf_score": 0.5}],
        )
    ]


async def test_cache_hit_emits_stored_answer_whole_no_agent_run(client, monkeypatch):
    hit = CachedAnswer(
        query="How do I set up MFA?",
        answer="Cached: enable MFA in settings.",
        citations=[{"article_id": "a1", "title": "MFA setup", "rrf_score": 0.5}],
        similarity=0.91,
    )
    session = FakeSession()
    monkeypatch.setattr(routes_chat, "_load_session", lambda sid: session)
    calls = install(monkeypatch, result=None, hit=hit)  # result=None: agent must not run

    resp = await client.post("/chat/stream", json={**BODY, "session_id": "s1"})
    events = parse_sse(resp.text)

    assert [kind for kind, _ in events] == ["delta", "final"]
    assert events[0][1] == {"text": hit.answer}  # ONE whole delta — no fake token-drip
    final = events[1][1]
    assert final["cached"] is True and final["answer"] == hit.answer
    assert set(final) == set(ChatResponse.model_fields)
    assert calls["run"] == [] and calls["store"] == []
    # The short-circuited turn still lands in the session (conversation stays coherent).
    assert [item["role"] for item in session.added] == ["user", "assistant"]


async def test_extraction_fires_after_the_stream_closes(client, monkeypatch):
    order: list[str] = []

    class OrderedResult(FakeStreamedResult):
        async def stream_events(self):
            async for event in super().stream_events():
                order.append("event")
                yield event

    result = OrderedResult(events=[delta("hi")], final_output="hi")
    calls = install(monkeypatch, result=result)

    async def fake_extract(user_id, message, session_id):
        order.append("extracted")
        calls["extract"].append((user_id, message, session_id))

    monkeypatch.setattr(routes_chat.extraction, "extract_and_store", fake_extract)

    resp = await client.post("/chat/stream", json={**BODY, "user_id": "demo.user@corp.com"})
    assert resp.status_code == 200
    assert parse_sse(resp.text)[-1][0] == "final"
    # Extraction ran exactly once, strictly after every streamed event (ADR-031: it can
    # never delay a token — it doesn't even start until the stream is closed).
    assert order == ["event", "extracted"]
    assert calls["extract"] == [("demo.user@corp.com", BODY["message"], None)]


async def test_error_mid_stream_emits_error_frame_and_no_partial_session_writes(
    client, monkeypatch
):
    session = FakeSession()
    monkeypatch.setattr(routes_chat, "_load_session", lambda sid: session)
    result = FakeStreamedResult(events=[delta("partial ")], boom=RuntimeError("model died"))
    calls = install(monkeypatch, result=result)

    resp = await client.post("/chat/stream", json={**BODY, "session_id": "s1"})
    events = parse_sse(resp.text)

    assert resp.status_code == 200  # committed before the failure — the error is in-band
    assert [kind for kind, _ in events] == ["delta", "error"]
    assert "RuntimeError" in events[-1][1]["detail"]
    assert not any(kind == "final" for kind, _ in events)
    # This handler wrote nothing partial: no assistant item, no cache store. (The SDK's own
    # session persistence is the same code for streamed and blocking runs — /chat parity.)
    assert session.added == [] and calls["store"] == []


async def test_slack_source_gets_no_chat_conveniences_on_the_stream_either(client, monkeypatch):
    """Gate symmetry with /chat (ADR-038/039): source=slack means no semantic cache, no
    memory extraction — the stream endpoint routes through the same source checks."""
    result = FakeStreamedResult(events=[delta("filed")], final_output="filed", agent="incident")
    calls = install(monkeypatch, result=result)
    looked_up: list[str] = []
    monkeypatch.setattr(
        routes_chat.semantic_cache, "lookup", lambda message: looked_up.append(message)
    )

    resp = await client.post("/chat/stream", json={**BODY, "source": "slack"})
    events = parse_sse(resp.text)

    assert [kind for kind, _ in events] == ["delta", "final"]
    assert looked_up == []  # never consulted
    assert calls["extract"] == [] and calls["store"] == []

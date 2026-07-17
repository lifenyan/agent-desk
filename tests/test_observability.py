"""M6 observability tests — all LLM-free and network-free (ADR-042…045).

What's pinned here:
- the NO-OP contract: empty keys => init_tracing registers NOTHING (the CI guarantee);
- the processor's trace/tag aggregation (ADR-043 schema) against fake SDK spans and a fake
  Langfuse client — intent/agent/cache_hit/flagged tags, token+cost rollup, session/user ids;
- the cost model: one price table, prefix-tolerant, unknown models are "unpriced" never $0;
- the cost budget alert (ADR-045): per-session accumulation in (fake) Redis, the Langfuse
  WARNING event, and the Redis-down per-trace fallback;
- the ADR-044 caches_disabled seam: all three caches call straight through, touch no Redis,
  and bump no stats counters (a deliberate OFF is not a miss);
- the eval trace-id hook: recorded id == the id the RunConfig pins the run to.
"""
# Implemented in M6.

from __future__ import annotations

import logging

from dotenv import load_dotenv

load_dotenv()

from agents.tracing.span_data import (  # noqa: E402
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
)

from app.cache import embedding_cache, response_cache, semantic_cache  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.observability.costs import cost_usd  # noqa: E402
from app.observability.tracing import LangfuseTracingProcessor  # noqa: E402
from evals.common import eval_run_config  # noqa: E402
from tests.conftest import FakeRedis  # noqa: E402

TRACE_ID = "trace_" + "a" * 32


# --- fakes -------------------------------------------------------------------------------------


class FakeOtelSpan:
    def __init__(self):
        self.attributes: dict = {}
        self.recording = True

    def is_recording(self):
        return self.recording

    def set_attribute(self, key, value):
        self.attributes[key] = value


class FakeObs:
    def __init__(self, name, as_type):
        self.name = name
        self.as_type = as_type
        self.updates: list[dict] = []
        self.children: list[FakeObs] = []
        self.events: list[dict] = []
        self.ended = False
        self._otel_span = FakeOtelSpan()

    def start_observation(self, *, name, as_type="span", **_):
        child = FakeObs(name, as_type)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def end(self):
        self.ended = True

    def create_event(self, **kwargs):
        self.events.append(kwargs)


class FakeLangfuse:
    def __init__(self):
        self.roots: list[FakeObs] = []
        self.flushed = 0

    def start_observation(self, *, trace_context=None, name, as_type="span", **_):
        root = FakeObs(name, as_type)
        root.trace_context = trace_context
        self.roots.append(root)
        return root

    def flush(self):
        self.flushed += 1


class FakeTrace:
    def __init__(self, trace_id=TRACE_ID, name="chat", group_id=None, metadata=None):
        self.trace_id = trace_id
        self.name = name
        self.group_id = group_id
        self.metadata = metadata


class FakeSpan:
    def __init__(self, span_data, span_id, parent_id=None, trace_id=TRACE_ID, error=None):
        self.span_data = span_data
        self.span_id = span_id
        self.parent_id = parent_id
        self.trace_id = trace_id
        self.error = error


def _run_spans(proc, spans_with_parents):
    """Start every span (in order), then end them in reverse — the SDK's nesting order."""
    spans = []
    for data, span_id, parent in spans_with_parents:
        span = FakeSpan(data, span_id, parent)
        proc.on_span_start(span)
        spans.append(span)
    for span in reversed(spans):
        proc.on_span_end(span)


# --- the no-op contract (CI has no keys and must stay green) ------------------------------------


def test_init_tracing_is_a_noop_without_keys(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    monkeypatch.setattr(tracing, "_client", None)
    registered = []
    monkeypatch.setattr(tracing, "add_trace_processor", registered.append)

    assert tracing.init_tracing() is False
    assert tracing.get_langfuse() is None
    assert registered == []
    tracing.flush()  # must be safe with no client


def test_init_tracing_registers_exactly_once(monkeypatch):
    monkeypatch.setattr(tracing, "get_langfuse", lambda: FakeLangfuse())
    monkeypatch.setattr(tracing, "_registered", False)
    registered = []
    monkeypatch.setattr(tracing, "add_trace_processor", registered.append)

    assert tracing.init_tracing() is True
    assert tracing.init_tracing() is True  # idempotent: create_app + evals in one process
    assert len(registered) == 1
    assert isinstance(registered[0], LangfuseTracingProcessor)


# --- the cost model (one table, never a silent $0) ----------------------------------------------


def test_cost_usd_prices_the_committed_models():
    assert cost_usd("gpt-5-mini", 1_000_000, 0) == 0.25
    assert cost_usd("gpt-5-mini", 0, 1_000_000) == 2.00
    assert cost_usd("gpt-5", 1_000_000, 1_000_000) == 11.25


def test_cost_usd_tolerates_dated_snapshots_but_not_unknown_models():
    assert cost_usd("gpt-5-mini-2025-08-07", 1_000_000, 0) == 0.25
    assert cost_usd("some-mystery-model", 1000, 1000) is None
    assert cost_usd(None, 1000, 1000) is None


# --- the processor's ADR-043 aggregation --------------------------------------------------------


def _chat_metadata(**overrides):
    return {"source": "chat", "user": "demo.user@corp.com", "cache_hit": "false", **overrides}


def test_processor_builds_tags_cost_and_session_from_a_run(monkeypatch):
    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    trace = FakeTrace(group_id="sess-1", metadata=_chat_metadata(input="how do I reset?"))

    proc.on_trace_start(trace)
    _run_spans(
        proc,
        [
            (AgentSpanData(name="router"), "span_1", None),
            (
                GenerationSpanData(
                    model="gpt-5-mini", usage={"input_tokens": 1000, "output_tokens": 100}
                ),
                "span_2",
                "span_1",
            ),
            (HandoffSpanData(from_agent="router", to_agent="knowledge"), "span_3", "span_1"),
            (AgentSpanData(name="knowledge"), "span_4", None),
            (
                FunctionSpanData(name="search_knowledge_articles", input="{}", output={"n": 3}),
                "span_5",
                "span_4",
            ),
            (
                GenerationSpanData(
                    model="gpt-5-mini", usage={"input_tokens": 3000, "output_tokens": 400}
                ),
                "span_6",
                "span_4",
            ),
        ],
    )
    proc.on_trace_end(trace)

    root = client.roots[0]
    assert root.ended
    assert root.trace_context == {"trace_id": "a" * 32}
    attrs = root._otel_span.attributes
    tags = set(attrs["langfuse.trace.tags"])
    assert {
        "source:chat",
        "intent:knowledge",  # first handoff target
        "agent:knowledge",  # last agent span
        "cache_hit:false",
        "flagged:false",
    } <= tags
    assert attrs["user.id"] == "demo.user@corp.com"
    assert attrs["session.id"] == "sess-1"
    assert attrs["langfuse.trace.metadata.input_tokens"] == "4000"
    assert attrs["langfuse.trace.metadata.output_tokens"] == "500"
    # 4000 in + 500 out on gpt-5-mini: (4000*0.25 + 500*2.00) / 1e6
    assert attrs["langfuse.trace.metadata.cost_usd"] == "0.002000"
    assert attrs["langfuse.trace.metadata.handoff_count"] == "1"
    # every child observation ended, one per SDK span
    assert len(root.children) == 2  # the two agent spans nest under the root
    assert all(obs.ended for obs in _walk(root))


def _walk(obs):
    for child in obs.children:
        yield child
        yield from _walk(child)


def test_processor_nests_children_under_their_parent_and_types_them():
    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    trace = FakeTrace()
    proc.on_trace_start(trace)
    _run_spans(
        proc,
        [
            (AgentSpanData(name="incident"), "span_1", None),
            (
                FunctionSpanData(name="create_ticket", input="{}", output={"id": "t1"}),
                "span_2",
                "span_1",
            ),
        ],
    )
    proc.on_trace_end(trace)

    root = client.roots[0]
    agent = root.children[0]
    assert (agent.name, agent.as_type) == ("incident", "agent")
    tool = agent.children[0]
    assert (tool.name, tool.as_type) == ("create_ticket", "tool")


def test_guardrail_tripwire_tags_the_trace_flagged():
    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    trace = FakeTrace(metadata=_chat_metadata(source="slack"))
    proc.on_trace_start(trace)
    _run_spans(
        proc,
        [
            (AgentSpanData(name="router"), "span_1", None),
            (
                GuardrailSpanData(name="slack_injection_guardrail", triggered=True),
                "span_2",
                "span_1",
            ),
        ],
    )
    proc.on_trace_end(trace)

    tags = set(client.roots[0]._otel_span.attributes["langfuse.trace.tags"])
    assert "flagged:true" in tags
    assert "source:slack" in tags


def test_cache_hit_trace_keeps_its_tag_and_reports_zero_tokens():
    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    trace = FakeTrace(group_id="sess-9", metadata=_chat_metadata(cache_hit="true"))
    proc.on_trace_start(trace)
    _run_spans(
        proc,
        [(CustomSpanData(name="semantic_cache_hit", data={"similarity": 0.91}), "span_1", None)],
    )
    proc.on_trace_end(trace)

    attrs = client.roots[0]._otel_span.attributes
    assert "cache_hit:true" in attrs["langfuse.trace.tags"]
    assert attrs["langfuse.trace.metadata.input_tokens"] == "0"
    assert attrs["langfuse.trace.metadata.cost_usd"] == "unpriced"  # no LLM ran — not $0


def test_processor_callbacks_never_raise_on_unknown_spans():
    proc = LangfuseTracingProcessor(FakeLangfuse())
    # spans for a trace the processor never saw (started before init): dropped, not raised
    orphan = FakeSpan(AgentSpanData(name="router"), "span_1", None, trace_id="trace_unknown")
    proc.on_span_start(orphan)
    proc.on_span_end(orphan)
    proc.on_trace_end(FakeTrace(trace_id="trace_unknown"))


# --- the cost budget alert (ADR-045) ------------------------------------------------------------


def _priced_trace_spans():
    # 30k in / 5k out on gpt-5-mini = $0.0175 per trace
    return [
        (AgentSpanData(name="fulfillment"), "span_1", None),
        (
            GenerationSpanData(
                model="gpt-5-mini", usage={"input_tokens": 30_000, "output_tokens": 5_000}
            ),
            "span_2",
            "span_1",
        ),
    ]


def test_cost_budget_accumulates_per_session_and_fires_event(monkeypatch, caplog):
    from app.cache import redis_client

    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "_client", fake)
    monkeypatch.setattr(get_settings(), "cost_alert_threshold_usd", 0.03)

    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    with caplog.at_level(logging.WARNING, logger="app.observability.tracing"):
        for _ in range(2):  # two $0.0175 turns of one conversation cross the $0.03 line
            trace = FakeTrace(group_id="sess-budget", metadata=_chat_metadata())
            proc.on_trace_start(trace)
            _run_spans(proc, _priced_trace_spans())
            proc.on_trace_end(trace)

    assert client.roots[0].events == []  # first turn: $0.0175 < $0.03
    events = client.roots[1].events
    assert len(events) == 1 and events[0]["name"] == "cost_budget_exceeded"
    assert "COST BUDGET EXCEEDED" in caplog.text
    assert fake.ttls  # the accumulator key got a TTL, it can't grow forever


def test_cost_budget_redis_down_degrades_to_per_trace(monkeypatch):
    from app.cache import redis_client

    from tests.conftest import DownRedis

    monkeypatch.setattr(redis_client, "_client", DownRedis())
    monkeypatch.setattr(get_settings(), "cost_alert_threshold_usd", 0.01)

    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    trace = FakeTrace(group_id="sess-x", metadata=_chat_metadata())
    proc.on_trace_start(trace)
    _run_spans(proc, _priced_trace_spans())
    proc.on_trace_end(trace)  # $0.0175 > $0.01 even without accumulation

    assert client.roots[0].events[0]["name"] == "cost_budget_exceeded"


def test_cost_budget_zero_threshold_disables(monkeypatch):
    monkeypatch.setattr(get_settings(), "cost_alert_threshold_usd", 0.0)
    client = FakeLangfuse()
    proc = LangfuseTracingProcessor(client)
    trace = FakeTrace(group_id="sess-y", metadata=_chat_metadata())
    proc.on_trace_start(trace)
    _run_spans(proc, _priced_trace_spans())
    proc.on_trace_end(trace)
    assert client.roots[0].events == []


# --- the ADR-044 caches_disabled seam -----------------------------------------------------------


def test_caches_disabled_embedding_calls_straight_through(monkeypatch, fake_redis):
    monkeypatch.setattr(get_settings(), "caches_disabled", True)
    calls = []
    monkeypatch.setattr(
        embedding_cache,
        "embed_texts",
        lambda texts, model=None: [[0.5] * 4 for _ in (calls.append(texts) or texts)],
    )
    out = embedding_cache.get_or_embed(["a", "b"], r=fake_redis)
    assert len(out) == 2
    assert calls == [["a", "b"]]  # embedded, not served
    assert fake_redis.store == {}  # nothing written: no vectors, no stats counters


def test_caches_disabled_semantic_lookup_and_store_are_inert(monkeypatch):
    from tests.conftest import DownRedis

    monkeypatch.setattr(get_settings(), "caches_disabled", True)
    # DownRedis raises on ANY use — passing it proves the flag short-circuits before Redis
    assert semantic_cache.lookup("how do I reset my password?", r=DownRedis()) is None
    assert semantic_cache.store("q", "a", [], r=DownRedis()) is False


def test_caches_disabled_response_cache_calls_through(monkeypatch, response_cache_redis):
    monkeypatch.setattr(get_settings(), "caches_disabled", True)
    calls = []

    @response_cache.cache_response(key_fn=lambda x: "fixed")
    def tool(x):
        calls.append(x)
        return {"value": x}

    assert tool(1) == {"value": 1}
    assert tool(1) == {"value": 1}
    assert calls == [1, 1]  # second call NOT served from cache
    assert response_cache_redis.store == {}  # and no entry, no stats, was written


# --- the eval trace-id hook (ADR-043) -----------------------------------------------------------


def test_eval_run_config_pins_the_recorded_trace_id():
    trace_id, run_config = eval_run_config("routing", "order me a laptop")
    assert len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id)
    assert run_config.trace_id == f"trace_{trace_id}"  # the row's id IS the run's trace
    assert run_config.workflow_name == "eval-routing"
    assert run_config.trace_metadata["case"] == "order me a laptop"


# --- route-table pin ----------------------------------------------------------------------------


def test_chat_route_is_still_bound_to_the_chat_handler():
    """Regression: an M6 helper inserted between the @router.post decorator and `async def
    chat` silently became the POST /chat handler (FastAPI decorates whatever follows) — every
    request 422'd. Caught live, pinned here: the route table is behavior, not layout."""
    from app.api.routes_chat import router

    chat_routes = [r for r in router.routes if getattr(r, "path", None) == "/chat"]
    assert len(chat_routes) == 1
    assert chat_routes[0].endpoint.__name__ == "chat"


def test_chat_stream_route_is_bound_to_its_handler():
    """Same pin for the M11 SSE endpoint — added the day the route was (the M6 incident's
    lesson applies to every helper-dense module, and routes_chat gained several in M11)."""
    from app.api.routes_chat import router

    stream_routes = [r for r in router.routes if getattr(r, "path", None) == "/chat/stream"]
    assert len(stream_routes) == 1
    assert stream_routes[0].endpoint.__name__ == "chat_stream"


def test_articles_route_is_bound_to_its_handler():
    """Same pin for the citation-link endpoint (the chat UI's article page depends on it)."""
    from app.api.routes_articles import router

    article_routes = [
        r for r in router.routes if getattr(r, "path", None) == "/articles/{article_id}"
    ]
    assert len(article_routes) == 1
    assert article_routes[0].endpoint.__name__ == "get_article"


class _FakeToolOutputItem:
    type = "tool_call_output_item"

    def __init__(self, output):
        self.output = output


def test_confirm_ticket_actions_appends_numbers_the_model_dropped():
    """Backstop for the observed 'I'll open a ticket now…' final message with the ticket
    already created: write-tool numbers absent from the answer get appended; reads never do."""
    from app.api.routes_chat import _confirm_ticket_actions

    items = [
        _FakeToolOutputItem({"ticket": {"number": "TKT313", "title": "t", "status": "open"}}),
        _FakeToolOutputItem({"comment": {"comment_id": "c", "ticket_number": "TKT042"}}),
        # get_ticket_status is a READ (comment fields mark it) — nothing to confirm:
        _FakeToolOutputItem(
            {"ticket": {"number": "TKT100", "status": "open", "latest_comment": None}}
        ),
        # duplicate via the string-encoded shape the SDK sometimes hands back:
        _FakeToolOutputItem('{"ticket": {"number": "TKT313", "status": "open"}}'),
    ]
    out = _confirm_ticket_actions("Understood — I'll open a ticket now.", items)
    assert "TKT313" in out and "TKT042" in out
    assert "TKT100" not in out
    assert out.count("TKT313") == 1

    already = "Done — created TKT313 and commented on TKT042."
    assert _confirm_ticket_actions(already, items) == already
    assert _confirm_ticket_actions("plain answer", []) == "plain answer"


def test_record_routes_are_bound_to_their_handlers():
    """Same pin for the ticket/order detail endpoints behind the UI's record links (ADR-046)."""
    from app.api.routes_records import router

    bound = {r.path: r.endpoint.__name__ for r in router.routes if hasattr(r, "endpoint")}
    assert bound == {"/tickets/{ref}": "ticket_detail", "/orders/{ref}": "order_detail"}

"""Langfuse tracing (M6, ADR-042/043): agent runs, handoffs, and tool calls become spans.

Design — one bridge, no second instrumentation layer:
- `LangfuseTracingProcessor` implements the Agents SDK's own `TracingProcessor` interface and
  is registered via `add_trace_processor`, so EVERY `Runner.run` in the process (routes_chat,
  guardrail classifier, memory extraction, eval suites) is traced with zero per-callsite work.
  Anything that starts an SDK trace by hand (the routes_chat cache-hit path uses
  `trace(...)` + `custom_span`) flows through the same bridge — Langfuse writing happens in
  exactly one place.
- Span callbacks only build local OTel span objects; the Langfuse SDK exports them from a
  background thread in batches. Nothing here adds a network call, retry, or timeout to the
  request path — the ADR-041 "chat is cost-free" property survives tracing by construction.
- Trace ids are shared: the SDK's `trace_<32 hex>` maps to the Langfuse trace id `<32 hex>`,
  so an eval row's recorded trace_id IS the Langfuse trace id (the dataset/cross-check join).

No-op contract (CI has no Langfuse secrets and must stay green): `init_tracing()` registers
nothing when either key is empty — no client, no processor, no network, never a crash. Keys
present but host unreachable degrades to background-export retries; requests are unaffected.

Trace-level tags/metadata written at trace end (the aggregation schema, ADR-043):
  tags:     source:{chat|slack|internal} · intent:{first handoff target} · agent:{last agent}
            · cache_hit:{true|false} · flagged:{true|false}
  metadata: input/output tokens, cost_usd (from app.observability.costs — the ONE table),
            handoff_count, plus whatever the caller passed in RunConfig.trace_metadata.
  user.id / session.id: from trace metadata "user" / the SDK trace group_id.

Cost budget (ADR-045): per-CONVERSATION spend accumulates in Redis (`cost:session:{id}`,
INCRBYFLOAT, 7-day TTL — same persistence pattern as cache stats). Crossing
`settings.cost_alert_threshold_usd` logs a loud warning AND attaches a WARNING-level Langfuse
event to the trace that crossed it. Redis down degrades to per-trace checks, silently —
a metric about spend must never fail a request.
"""
# Implemented in M6 (was the last TODO stub). The M3 /cache/stats counters stay the source of
# cache hit rates; scripts/export_metrics.py bridges them next to the Langfuse numbers.

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import redis
from agents.tracing import add_trace_processor
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.span_data import (
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    ResponseSpanData,
)
from agents.tracing.spans import Span
from agents.tracing.traces import Trace

from app.config import get_settings
from app.observability.costs import cost_usd

logger = logging.getLogger(__name__)

_TRUNCATE_CHARS = 20_000  # keep pathological payloads (thread envelopes) out of the exporter

_SESSION_COST_PREFIX = "cost:session:"
_SESSION_COST_TTL = 7 * 86_400


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _TRUNCATE_CHARS:
        return value[:_TRUNCATE_CHARS] + f"… [truncated, {len(value)} chars]"
    return value


def _langfuse_trace_id(sdk_trace_id: str) -> str:
    """SDK `trace_<32 hex>` -> W3C 32-hex Langfuse trace id, shared verbatim so eval rows and
    Langfuse agree on the id. Anything nonstandard is hashed into a valid id instead."""
    from langfuse import Langfuse

    candidate = sdk_trace_id.removeprefix("trace_").lower()
    if len(candidate) == 32 and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    return Langfuse.create_trace_id(seed=sdk_trace_id)


class _TraceState:
    """Everything the trace-end aggregation needs, collected as spans finish."""

    __slots__ = ("root", "observations", "agents", "handoffs", "usage", "flagged", "last_output")

    def __init__(self, root: Any):
        self.root = root
        self.observations: dict[str, Any] = {}  # sdk span_id -> Langfuse observation
        self.agents: list[str] = []  # agent span names in start order
        self.handoffs: list[str] = []  # handoff targets in end order
        self.usage: list[tuple[str | None, int, int]] = []  # (model, in_tokens, out_tokens)
        self.flagged = False  # any guardrail span with triggered=True
        self.last_output: str | None = None  # last LLM output text -> trace output


class LangfuseTracingProcessor(TracingProcessor):
    """Bridges SDK traces/spans to Langfuse observations 1:1, live (real timestamps).

    Every callback is wrapped: a tracing bug must degrade to a lost span, never a failed
    request (the SDK calls these synchronously inside agent runs).
    """

    def __init__(self, client: Any):
        self._client = client
        self._lock = threading.Lock()
        self._traces: dict[str, _TraceState] = {}

    # -- trace lifecycle ------------------------------------------------------------------

    def on_trace_start(self, trace: Trace) -> None:
        try:
            root = self._client.start_observation(
                trace_context={"trace_id": _langfuse_trace_id(trace.trace_id)},
                name=trace.name,
                as_type="span",
            )
            with self._lock:
                self._traces[trace.trace_id] = _TraceState(root)
        except Exception:  # noqa: BLE001 — tracing must never break the traced request
            logger.warning("langfuse: on_trace_start failed", exc_info=True)

    def on_trace_end(self, trace: Trace) -> None:
        try:
            with self._lock:
                state = self._traces.pop(trace.trace_id, None)
            if state is None:
                return
            self._finalize_trace(trace, state)
        except Exception:  # noqa: BLE001
            logger.warning("langfuse: on_trace_end failed", exc_info=True)

    # -- span lifecycle -------------------------------------------------------------------

    def on_span_start(self, span: Span[Any]) -> None:
        try:
            with self._lock:
                state = self._traces.get(span.trace_id)
                parent = state.observations.get(span.parent_id, state.root) if state else None
            if state is None:
                return
            name, as_type = self._name_and_type(span.span_data)
            obs = parent.start_observation(name=name, as_type=as_type)
            with self._lock:
                state.observations[span.span_id] = obs
                if isinstance(span.span_data, AgentSpanData):
                    state.agents.append(span.span_data.name)
        except Exception:  # noqa: BLE001
            logger.warning("langfuse: on_span_start failed", exc_info=True)

    def on_span_end(self, span: Span[Any]) -> None:
        try:
            with self._lock:
                state = self._traces.get(span.trace_id)
                obs = state.observations.get(span.span_id) if state else None
            if state is None or obs is None:
                return
            self._apply_span_data(obs, span, state)
            if span.error:
                obs.update(
                    level="ERROR",
                    status_message=str(span.error.get("message", "error"))
                    if isinstance(span.error, dict)
                    else str(span.error),
                )
            obs.end()
        except Exception:  # noqa: BLE001
            logger.warning("langfuse: on_span_end failed", exc_info=True)

    def shutdown(self) -> None:
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001
            logger.warning("langfuse: flush on shutdown failed", exc_info=True)

    def force_flush(self) -> None:
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001
            logger.warning("langfuse: force_flush failed", exc_info=True)

    # -- per-span-type mapping ------------------------------------------------------------

    @staticmethod
    def _name_and_type(data: Any) -> tuple[str, str]:
        if isinstance(data, AgentSpanData):
            return data.name, "agent"
        if isinstance(data, FunctionSpanData):
            return data.name, "tool"
        if isinstance(data, (GenerationSpanData, ResponseSpanData)):
            return "llm-call", "generation"
        if isinstance(data, HandoffSpanData):
            return "handoff", "span"
        if isinstance(data, GuardrailSpanData):
            return data.name, "guardrail"
        if isinstance(data, CustomSpanData):
            return data.name, "span"
        return getattr(data, "type", "span"), "span"

    def _apply_span_data(self, obs: Any, span: Span[Any], state: _TraceState) -> None:
        """Copy the finished SDK span's payload onto the Langfuse observation and feed the
        trace-level aggregation (usage, handoffs, guardrail verdicts)."""
        data = span.span_data
        if isinstance(data, AgentSpanData):
            obs.update(metadata={"handoffs": data.handoffs, "tools": data.tools})
        elif isinstance(data, FunctionSpanData):
            obs.update(input=_truncate(data.input), output=_truncate(str(data.output)))
        elif isinstance(data, (GenerationSpanData, ResponseSpanData)):
            self._apply_llm_span(obs, data, state)
        elif isinstance(data, HandoffSpanData):
            obs.update(
                name=f"handoff → {data.to_agent}",
                metadata={"from_agent": data.from_agent, "to_agent": data.to_agent},
            )
            if data.to_agent:
                state.handoffs.append(data.to_agent)
        elif isinstance(data, GuardrailSpanData):
            obs.update(
                metadata={"triggered": data.triggered},
                level="WARNING" if data.triggered else None,
            )
            if data.triggered:
                state.flagged = True
        elif isinstance(data, CustomSpanData):
            obs.update(metadata=data.data)

    def _apply_llm_span(self, obs: Any, data: Any, state: _TraceState) -> None:
        usage: dict | None = getattr(data, "usage", None)
        model: str | None = None
        output_text: str | None = None
        if isinstance(data, ResponseSpanData):
            response = data.response
            if response is not None:
                model = getattr(response, "model", None)
                output_text = getattr(response, "output_text", None) or None
            obs.update(input=_truncate(json.dumps(data.input, default=str)) if data.input else None)
        else:  # GenerationSpanData (LiteLLM path)
            model = data.model
            if data.output:
                output_text = _truncate(json.dumps(data.output, default=str))
            if data.input:
                obs.update(input=_truncate(json.dumps(data.input, default=str)))

        in_tokens = int(usage.get("input_tokens") or 0) if usage else 0
        out_tokens = int(usage.get("output_tokens") or 0) if usage else 0
        cost = cost_usd(model, in_tokens, out_tokens)
        obs.update(
            model=model,
            output=_truncate(output_text),
            usage_details={"input": in_tokens, "output": out_tokens} if usage else None,
            cost_details={"total": cost} if cost is not None else None,
        )
        if usage:
            state.usage.append((model, in_tokens, out_tokens))
        if output_text:
            state.last_output = output_text

    # -- trace-end aggregation (the ADR-043 tag schema) -------------------------------------

    def _finalize_trace(self, trace: Trace, state: _TraceState) -> None:
        meta: dict[str, Any] = dict(getattr(trace, "metadata", None) or {})
        group_id: str | None = getattr(trace, "group_id", None)

        in_tokens = sum(u[1] for u in state.usage)
        out_tokens = sum(u[2] for u in state.usage)
        costs = [cost_usd(m, i, o) for m, i, o in state.usage]
        total_cost = (
            sum(c for c in costs if c is not None) if any(c is not None for c in costs) else None
        )

        last_agent = state.agents[-1] if state.agents else None
        intent = state.handoffs[0] if state.handoffs else last_agent
        cache_hit = str(meta.get("cache_hit", "false")).lower() == "true"
        tags = [
            f"source:{meta.get('source', 'internal')}",
            f"intent:{intent or 'none'}",
            f"agent:{last_agent or 'none'}",
            f"cache_hit:{str(cache_hit).lower()}",
            f"flagged:{str(state.flagged).lower()}",
        ]
        trace_meta = {
            **{k: str(v) for k, v in meta.items()},
            "input_tokens": str(in_tokens),
            "output_tokens": str(out_tokens),
            "cost_usd": f"{total_cost:.6f}" if total_cost is not None else "unpriced",
            "handoff_count": str(len(state.handoffs)),
        }

        # Trace-level attributes are plain OTel attributes Langfuse reads off any span of the
        # trace (see langfuse._client.attributes) — the root span is ours, so set them there.
        # LangfuseOtelSpanAttributes is part of the public langfuse API surface; only the
        # attribute WRITE goes through the OTel span (there is no public setter for a span
        # that isn't the ambient context span).
        from langfuse import LangfuseOtelSpanAttributes as A

        otel_span = state.root._otel_span  # noqa: SLF001 — see comment above
        if otel_span.is_recording():
            otel_span.set_attribute(A.TRACE_NAME, trace.name)
            otel_span.set_attribute(A.TRACE_TAGS, tags)
            if meta.get("user"):
                otel_span.set_attribute(A.TRACE_USER_ID, str(meta["user"]))
            if group_id:
                otel_span.set_attribute(A.TRACE_SESSION_ID, group_id)
            for key, value in trace_meta.items():
                otel_span.set_attribute(f"{A.TRACE_METADATA}.{key}", value)
            if meta.get("input"):
                otel_span.set_attribute(A.TRACE_INPUT, str(meta["input"]))
            if state.last_output:
                otel_span.set_attribute(A.TRACE_OUTPUT, _truncate(state.last_output))

        if total_cost is not None:
            self._check_cost_budget(state.root, group_id, total_cost)
        state.root.end()

    def _check_cost_budget(self, root: Any, session_id: str | None, trace_cost: float) -> None:
        """ADR-045: warn LOUDLY when a conversation's cumulative spend crosses the threshold.

        Cumulative = Redis INCRBYFLOAT per session (survives restarts, shared across workers,
        7-day TTL); Redis down degrades to judging this trace alone."""
        threshold = get_settings().cost_alert_threshold_usd
        if threshold <= 0:
            return
        conversation_cost = trace_cost
        if session_id:
            try:
                from app.cache.redis_client import get_redis

                key = f"{_SESSION_COST_PREFIX}{session_id}".encode()
                r = get_redis()
                conversation_cost = float(r.incrbyfloat(key, trace_cost))
                r.expire(key, _SESSION_COST_TTL)
            except redis.RedisError:
                pass  # per-trace fallback; a spend metric must never fail a request
        if conversation_cost > threshold:
            logger.warning(
                "COST BUDGET EXCEEDED: conversation %s at $%.4f (threshold $%.2f, "
                "this trace $%.4f)",
                session_id or "<no session>",
                conversation_cost,
                threshold,
                trace_cost,
            )
            root.create_event(
                name="cost_budget_exceeded",
                level="WARNING",
                metadata={
                    "conversation_cost_usd": f"{conversation_cost:.6f}",
                    "threshold_usd": f"{threshold:.2f}",
                    "trace_cost_usd": f"{trace_cost:.6f}",
                    "session_id": session_id or "",
                },
            )


# --- module-level lifecycle -------------------------------------------------------------------

_client: Any = None
_registered = False


def tracing_enabled() -> bool:
    settings = get_settings()
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def get_langfuse() -> Any:
    """The shared Langfuse client, or None when tracing is off (keys empty).

    Import stays inside: with keys unset the langfuse package (and its OTel stack) is never
    even imported — the CI no-op contract at its cheapest.
    """
    global _client
    if not tracing_enabled():
        return None
    if _client is None:
        from langfuse import Langfuse

        settings = get_settings()
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _client


def init_tracing() -> bool:
    """Register the Langfuse processor once per process; False = keys empty, clean no-op.

    Idempotent on purpose: create_app() and evals/run_evals.py both call this, and tests may
    build several apps in one process — the SDK must never see two bridges (double spans).
    """
    global _registered
    client = get_langfuse()
    if client is None:
        logger.info("langfuse tracing OFF (no keys configured) — running untraced")
        return False
    if _registered:
        return True
    add_trace_processor(LangfuseTracingProcessor(client))
    _registered = True
    logger.info("langfuse tracing ON (host %s)", get_settings().langfuse_host)
    return True


def flush() -> None:
    """Drain the background exporter — call before short-lived processes exit (evals, tests)."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:  # noqa: BLE001
            logger.warning("langfuse flush failed", exc_info=True)

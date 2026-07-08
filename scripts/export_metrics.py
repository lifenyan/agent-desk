"""Headline observability numbers (M6, requirement 3): one read-only table from Langfuse + Redis.

Reads traces back from the Langfuse API (the same ones the ADR-043 bridge wrote) and prints
the numbers the README metrics section quotes:
  - p50/p95 end-to-end latency, split cache-hit vs miss (chat traces; the cache_hit tag)
  - p50/p95 latency per specialist agent (the agent:* tag)
  - tokens + cost per conversation (traces grouped by Langfuse session = chat session_id —
    this is also what closes the ADR-034 gap: e2e/slack flows bill inside the spawned server,
    invisible to the harness over HTTP, but their traces carry session ids and cost)
  - cost per resolved request (mean/total over non-flagged chat traces; flagged runs did no work)
  - handoff-count distribution (the ADR-003 ping-pong number, now from live traffic)
  - hit rates for all three caches — BRIDGED from the M3 Redis counters (`/cache/stats`'s own
    `stats.snapshot()`), not recomputed: Langfuse sees traces, the counters see every cache
    decision including ingest-time embedding hits. Two sources, labeled as such.

Read-only by design: no Streamlit, no fourth service — Langfuse's own UI is the dashboard;
this script exists for the README table and for terminals. Usage:
    python scripts/export_metrics.py [--hours 24] [--env-name chat] [--markdown]
Requires LANGFUSE_* keys (exits 2 with a clear message otherwise).
"""
# Implemented in M6 (ADR-043).

from __future__ import annotations

import argparse
import datetime
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv

load_dotenv()

from app.cache import stats as cache_stats  # noqa: E402
from app.observability import tracing  # noqa: E402


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank, same convention as evals/common.py — numbers must be comparable."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _tag(trace, prefix: str) -> str | None:
    for t in trace.tags or []:
        if t.startswith(prefix):
            return t.removeprefix(prefix)
    return None


def fetch_traces(client, hours: float, names: tuple[str, ...]) -> list:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
    out = []
    for name in names:
        page = 1
        while True:
            resp = client.api.trace.list(name=name, from_timestamp=since, page=page, limit=100)
            out.extend(resp.data)
            if page >= resp.meta.total_pages:
                break
            page += 1
    return out


def _fmt_p(values: list[float]) -> str:
    p50, p95 = _percentile(values, 50), _percentile(values, 95)
    if p50 is None:
        return "— (no traces)"
    return f"{p50:.2f}s / {p95:.2f}s  (n={len(values)})"


def _meta_float(trace, key: str) -> float | None:
    try:
        return float((trace.metadata or {}).get(key))
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0, help="look-back window")
    parser.add_argument(
        "--names",
        nargs="+",
        default=["chat"],
        help="trace (workflow) names to aggregate; default: the chat surface only",
    )
    args = parser.parse_args()

    client = tracing.get_langfuse()
    if client is None:
        print("LANGFUSE keys are not configured — nothing to export.", file=sys.stderr)
        return 2

    traces = fetch_traces(client, args.hours, tuple(args.names))
    if not traces:
        print(f"no traces named {args.names} in the last {args.hours}h", file=sys.stderr)
        return 1

    lat_by_cache: dict[str, list[float]] = defaultdict(list)
    lat_by_agent: dict[str, list[float]] = defaultdict(list)
    cost_by_session: dict[str, float] = defaultdict(float)
    tokens_by_session: dict[str, int] = defaultdict(int)
    handoff_counts: Counter[int] = Counter()
    resolved_costs: list[float] = []
    flagged = 0

    for t in traces:
        latency = t.latency  # seconds end-to-end, computed by Langfuse from the span tree
        cache_hit = _tag(t, "cache_hit:") == "true"
        agent = _tag(t, "agent:")
        is_flagged = _tag(t, "flagged:") == "true"
        cost = t.total_cost if t.total_cost else (_meta_float(t, "cost_usd") or 0.0)
        in_tok = _meta_float(t, "input_tokens") or 0
        out_tok = _meta_float(t, "output_tokens") or 0

        if latency is not None:
            lat_by_cache["hit" if cache_hit else "miss"].append(latency)
            if agent and agent != "none" and not cache_hit:
                lat_by_agent[agent].append(latency)
        session = t.session_id or t.id
        cost_by_session[session] += cost
        tokens_by_session[session] += int(in_tok + out_tok)
        hc = _meta_float(t, "handoff_count")
        if hc is not None:
            handoff_counts[int(hc)] += 1
        if is_flagged:
            flagged += 1
        else:
            resolved_costs.append(cost)

    total_cost = sum(cost_by_session.values())
    session_costs = list(cost_by_session.values())
    session_tokens = list(tokens_by_session.values())

    print(
        f"\n## Headline metrics — {len(traces)} traces named {args.names}, last {args.hours:g}h\n"
    )
    print("| metric | value |")
    print("|---|---|")
    print(f"| e2e latency p50/p95 — cache HIT | {_fmt_p(lat_by_cache['hit'])} |")
    print(f"| e2e latency p50/p95 — cache MISS (agents ran) | {_fmt_p(lat_by_cache['miss'])} |")
    for agent in sorted(lat_by_agent):
        print(f"| latency p50/p95 — {agent} runs | {_fmt_p(lat_by_agent[agent])} |")
    print(
        f"| tokens per conversation p50/p95 | "
        f"{_percentile(session_tokens, 50):.0f} / {_percentile(session_tokens, 95):.0f} "
        f"({len(session_tokens)} conversations) |"
    )
    print(
        f"| cost per conversation p50/p95 | "
        f"${_percentile(session_costs, 50):.4f} / ${_percentile(session_costs, 95):.4f} |"
    )
    if resolved_costs:
        print(
            f"| cost per resolved request (mean) | ${sum(resolved_costs) / len(resolved_costs):.4f} "
            f"({len(resolved_costs)} requests; {flagged} flagged excluded) |"
        )
    print(f"| total cost in window | ${total_cost:.4f} |")
    dist = ", ".join(f"{k} hops×{v}" for k, v in sorted(handoff_counts.items())) or "—"
    print(f"| handoff-count distribution | {dist} |")

    # The M3 counters (bridged, not rebuilt): every cache decision since counter creation —
    # a different window than the traces above, and the only source that sees ingest-time
    # embedding traffic. Labeled lifetime for that reason.
    try:
        snapshot = cache_stats.snapshot()
        for name, s in snapshot.items():
            rate = f"{s['hit_rate']:.1%}" if s["hit_rate"] is not None else "no traffic"
            print(
                f"| {name} cache hit rate (lifetime counters) | {rate} "
                f"({s['hits']} hits / {s['misses']} misses) |"
            )
    except Exception:  # noqa: BLE001
        print("| cache hit rates | Redis unreachable — see GET /cache/stats |")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

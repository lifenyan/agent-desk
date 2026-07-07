"""Shared eval-harness plumbing: dataset loading, the eval acting user, floors, cost/latency.

Floors live in evals/thresholds.toml (single source of truth, ADR-026) — the harness reads
them here and CI runs the same harness, so there is no second copy of the numbers anywhere.

M5 added per-case cost + latency (ADR-034): every suite row carries wall-clock latency_s and,
where the case is an SDK run, token counts + cost from the run's Usage. Cost is tokens ×
the committed price table below — the SDK reports tokens, never dollars. This is the number
M6 will cross-check against Langfuse traces.
"""
# Implemented in M4 (extracted from run_evals.py so the e2e/dedup suite modules can share it
# without importing the CLI module). M5 added the price table + usage/latency helpers.

from __future__ import annotations

import json
import tomllib
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "datasets"

# Suites that exercise ACTION agents (routing, e2e, dedup) act as this (seeded) user.
EVAL_USER = "demo.user@corp.com"

# USD per 1M tokens (input, output) — checked 2026-07-07 against the OpenAI pricing page.
# Only the models the harness actually bills: the pinned workhorse pair (ADR-026) + the judge
# (ADR-033). A model missing here makes cost None rather than silently $0.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_floors() -> dict:
    with (Path(__file__).parent / "thresholds.toml").open("rb") as f:
        return tomllib.load(f)


FLOORS = load_floors()


def usage_fields(result, model: str) -> dict:
    """Per-case tokens + cost from an SDK run result's aggregated Usage.

    `model` is the model every LLM call in the run billed against (all current suites run a
    single-model stack; multi-model runs would need per-request usage entries instead).
    """
    usage = result.context_wrapper.usage
    prices = PRICES_PER_MTOK.get(model)
    cost = (
        (usage.input_tokens * prices[0] + usage.output_tokens * prices[1]) / 1_000_000
        if prices
        else None
    )
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "requests": usage.requests,
        "cost_usd": round(cost, 6) if cost is not None else None,
    }


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (pct in [0,100]); None on empty input. Kept dependency-free —
    12-to-40-case suites don't need interpolation subtleties."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def cost_latency_aggregates(rows: list[dict]) -> dict:
    """Suite-level cost/latency block from per-case rows: totals + p50/p95 wall-clock.
    Rows without cost (LLM-free or HTTP-side cases) count toward latency only."""
    latencies = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    return {
        "total_cost_usd": round(sum(costs), 4) if costs else None,
        "cases_with_cost": len(costs),
        "total_latency_s": round(sum(latencies), 1) if latencies else None,
        "latency_p50_s": round(percentile(latencies, 50), 2) if latencies else None,
        "latency_p95_s": round(percentile(latencies, 95), 2) if latencies else None,
    }

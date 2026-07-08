"""The ONE price table (ADR-034/042): model -> USD per 1M tokens, and the cost helper.

Moved here from evals/common.py in M6 so the eval harness and the Langfuse trace processor
price tokens from the SAME numbers — the M5 rule ("cost is tokens x the committed table, the
SDK reports tokens never dollars") now has a single home that both consumers import. A second
copy would let the cross-check (harness cost vs Langfuse cost) drift into agreeing with itself.

Only the models this stack actually bills: the pinned workhorse pair (ADR-026 — CI floors bind
to these, never swap models) plus the M5 eval judge. A model missing here yields cost None
rather than a silent $0 — an unknown model must show up as "unpriced", not "free".
"""
# Implemented in M6 (relocated from evals/common.py, where M5 introduced it — ADR-034).

from __future__ import annotations

# USD per 1M tokens (input, output) — checked 2026-07-07 against the OpenAI pricing page.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
}


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Dollar cost of one model call, or None when the model has no committed price.

    Bare LiteLLM names only ("gpt-5-mini"); response objects sometimes report a dated
    snapshot ("gpt-5-mini-2025-08-07") — match on the committed prefix so a snapshot
    rename doesn't silently unprice every trace.
    """
    if not model:
        return None
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        for name, p in PRICES_PER_MTOK.items():
            if model.startswith(name + "-"):
                prices = p
                break
    if prices is None:
        return None
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000

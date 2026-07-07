"""Eval metrics: recall@k, MRR, routing accuracy, handoff ping-pong rate.

Pure functions over id lists — no I/O, no LLM — so they are unit-testable and reusable across
suites. Retrieval metrics are computed at the ARTICLE level: the dataset's expected ids are
article ids, so chunk hits are collapsed to their parent article (first occurrence keeps the
rank) before scoring.
"""
# recall@k + MRR implemented in M1. TODO(M4): routing accuracy, handoff ping-pong rate.

from __future__ import annotations

from collections.abc import Sequence


def dedupe_preserving_order(ids: Sequence[str]) -> list[str]:
    """Collapse chunk-level results to unique ids, keeping each id's best (first) rank."""
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def recall_at_k(expected: Sequence[str], retrieved: Sequence[str], k: int = 5) -> float:
    """Fraction of expected ids present in the top-k retrieved (deduped, order-preserving)."""
    if not expected:
        raise ValueError(
            "recall@k is undefined for an empty expected set (refusal cases score separately)"
        )
    top = set(dedupe_preserving_order(retrieved)[:k])
    return sum(1 for e in expected if e in top) / len(expected)


def mrr(expected: Sequence[str], retrieved: Sequence[str]) -> float:
    """Reciprocal rank of the FIRST relevant id (0.0 if none retrieved)."""
    expected_set = set(expected)
    for rank, rid in enumerate(dedupe_preserving_order(retrieved), start=1):
        if rid in expected_set:
            return 1.0 / rank
    return 0.0

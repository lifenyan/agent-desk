"""Langfuse dataset wiring (M6, ADR-043 — moved here from the original M5 plan).

Purpose: make eval runs comparable OVER TIME in Langfuse. Each wired suite becomes a Langfuse
dataset (`agentdesk-<suite>`), each case a dataset item with a DETERMINISTIC id (sha1 of
suite + query — re-runs upsert the same item instead of multiplying it), and each harness run
a dataset RUN whose items link to the exact trace the case produced (the ADR-043 trace-id
hook). In the Langfuse UI: Datasets -> agentdesk-<suite> -> compare runs side by side.

Wired suites: retrieval (the refusal slice — the answerable slice is LLM-free by design and
produces no trace to link) and routing. The nightly-only suites keep their evidence in the
harness JSON (`--out`) + tagged traces; linking them is mechanical if ever wanted (same three
calls), deliberately not paid for now.

No-op contract: without Langfuse keys every function returns immediately — the harness output
is byte-identical with tracing on or off, and CI (no secrets) never notices this module.
"""
# Implemented in M6.

from __future__ import annotations

import datetime
import hashlib
import logging

from app.observability import tracing

logger = logging.getLogger(__name__)


def _item_id(suite: str, query: str) -> str:
    return hashlib.sha1(f"{suite}\x00{query}".encode()).hexdigest()


def record_dataset_run(suite: str, rows: list[dict], run_name: str | None = None) -> str | None:
    """Upsert this suite's cases as dataset items and link this run's traces; returns the
    run name, or None when tracing is off / nothing was linkable. Never raises: dataset
    bookkeeping must not fail an eval run that already produced its numbers."""
    client = tracing.get_langfuse()
    if client is None:
        return None
    linkable = [r for r in rows if r.get("trace_id")]
    if not linkable:
        return None
    run_name = run_name or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    dataset = f"agentdesk-{suite}"
    try:
        client.create_dataset(name=dataset)  # upsert by name
        for r in linkable:
            item_id = _item_id(suite, r["query"])
            client.create_dataset_item(
                dataset_name=dataset,
                id=item_id,  # deterministic => upsert, never a duplicate case
                input=r["query"],
                expected_output=r.get("expected", "refusal"),
                metadata={"suite": suite},
            )
            client.api.dataset_run_items.create(
                run_name=run_name,
                dataset_item_id=item_id,
                trace_id=r["trace_id"],
                metadata={"pass": bool(r.get("pass", r.get("correct", False)))},
            )
        logger.info(
            "langfuse dataset %s: linked %d cases as run %r", dataset, len(linkable), run_name
        )
        return run_name
    except Exception:  # noqa: BLE001 — bookkeeping, not evidence: log and move on
        logger.warning("langfuse dataset wiring failed for %s", suite, exc_info=True)
        return None

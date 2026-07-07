"""Dedup suite (M4, ADR-028): measures the ADR-021 GRAY BAND the 0.80 flag can't cover.

ADR-021's finding: a fresh report of an already-ticketed issue scores only 0.59–0.77 cosine
against its true group — inside the cross-issue nearest-neighbor range — so no threshold
separates "same issue, new report" from "similar but different issue". Below 0.80 the decision
is the INCIDENT AGENT's judgment, and until this suite that judgment zone was unevaluated
(honest-accounting debt from M2, formalized from ignore/tem/m2_dedup_sweep.py's probes).

Design:
- link probes: user-phrased fresh reports of seeded duplicate groups that have OPEN tickets —
  the agent should LINK (add_ticket_comment on a ticket of that group), not create.
- trap probes: similar-but-different issues (same device/domain as a seeded group, different
  failure) — the agent should CREATE a new ticket; linking would lose the report.
- runs through the incident agent DIRECTLY with real tools against the seeded DB (like the
  routing suite — Runner.run, bypassing routes_chat so the semantic cache can't interfere),
  scored on the ACTION taken as read from the DB, never on the answer text.
- per-case cleanup: each probe's created rows are deleted before the next probe runs, so an
  earlier probe's fresh ticket can never become a later probe's dedup candidate.

The FIRST run of this suite establishes the baseline; the floor in thresholds.toml is then set
below it (regression gate, ADR-026). Per ADR-028 this suite is the evidence base for the
deferred cross-encoder / pair-judge decision — if the gray band scores poorly, that is a
finding to report, not a failure to hide.
"""
# Implemented in M4.

from __future__ import annotations

import asyncio
import json
import time

from agents import Runner
from sqlalchemy import select

from app.agents.context import ChatContext
from app.agents.incident import incident_agent
from app.config import get_settings
from app.db.database import SessionLocal
from app.db.models import Ticket, TicketComment
from evals.common import (
    DATASET_DIR,
    EVAL_USER,
    FLOORS,
    cost_latency_aggregates,
    load_jsonl,
    usage_fields,
)


def _max_search_similarity(result) -> float | None:
    """Best candidate similarity the agent's search_similar_tickets call saw — evidence that
    the probe actually lives in the gray band (best-effort; None if unparseable)."""
    best = None
    for item in result.new_items:
        if getattr(item, "type", None) != "tool_call_output_item":
            continue
        output = item.output
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                continue
        if isinstance(output, dict) and "candidates" in output:
            for c in output["candidates"]:
                sim = c.get("similarity")
                if sim is not None and (best is None or sim > best):
                    best = sim
    return best


async def _run_probe(case: dict) -> dict:
    with SessionLocal() as s:
        before_tickets = set(s.scalars(select(Ticket.id)))
        before_comments = set(s.scalars(select(TicketComment.id)))

    top_similarity = None
    error = None
    usage = {"cost_usd": None}
    t0 = time.perf_counter()
    try:
        result = await Runner.run(
            incident_agent, case["report"], context=ChatContext(user_id=EVAL_USER)
        )
        top_similarity = _max_search_similarity(result)
        usage = usage_fields(result, get_settings().specialist_model)
    except Exception as exc:  # noqa: BLE001 — a crashed run scores as a failed action
        error = f"{exc.__class__.__name__}: {exc}"
    latency_s = round(time.perf_counter() - t0, 2)

    with SessionLocal() as s:
        new_ticket_ids = set(s.scalars(select(Ticket.id))) - before_tickets
        new_comments = list(
            s.scalars(select(TicketComment).where(TicketComment.id.notin_(before_comments)))
        )
        commented_titles = [s.get(Ticket, c.ticket_id).title for c in new_comments]

        # Action precedence: creating a ticket IS the decision "not a duplicate", even if the
        # agent also commented somewhere.
        if error is not None:
            action = "error"
        elif new_ticket_ids:
            action = "created"
        elif new_comments:
            action = "linked"
        else:
            action = "none"

        # Per-case cleanup BEFORE the next probe runs (see module docstring).
        for c in new_comments:
            s.delete(c)
        for t in s.scalars(select(Ticket).where(Ticket.id.in_(new_ticket_ids))):
            s.delete(t)
        s.commit()

    if case["expect"] == "link":
        # `group` may list several acceptable targets: distinct seeded groups can describe the
        # same user-visible outage (e.g. "DNS not resolving" vs "can't reach internal site").
        groups = case["group"] if isinstance(case["group"], list) else [case["group"]]
        ok = action == "linked" and any(g in commented_titles for g in groups)
    else:  # trap: expect a NEW ticket
        ok = action == "created"

    return {
        "report": case["report"],
        "kind": case["expect"],
        "group": str(case.get("group") or case.get("trap_neighbor", "")),
        "action": action,
        "linked_to": commented_titles[0] if commented_titles else None,
        "top_similarity": top_similarity,
        "ok": ok,
        "error": error,
        "latency_s": latency_s,
        **usage,
    }


def run_dedup(**_ignored) -> dict:
    """Run the dedup suite; retrieval-suite kwargs (k/threshold/refusal_mode) don't apply."""
    cases = load_jsonl(DATASET_DIR / "dedup.jsonl")

    async def _all() -> list[dict]:
        return [await _run_probe(case) for case in cases]  # sequential: probes must not overlap

    rows = asyncio.run(_all())

    links = [r for r in rows if r["kind"] == "link"]
    traps = [r for r in rows if r["kind"] == "new"]
    report = {
        "suite": "dedup",
        "rows": rows,
        "aggregates": {
            "link_accuracy": sum(r["ok"] for r in links) / len(links) if links else None,
            "trap_accuracy": sum(r["ok"] for r in traps) / len(traps) if traps else None,
            "accuracy": sum(r["ok"] for r in rows) / len(rows),
            "n_link": len(links),
            "n_trap": len(traps),
        },
    }
    report["cost_latency"] = cost_latency_aggregates(rows)
    report["passed"] = report["aggregates"]["accuracy"] >= FLOORS["dedup"]["accuracy"]
    return report

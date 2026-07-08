"""Graph suite (M9, ADR-036): plain RAG vs Graph-RAG on multi-hop questions — the flagship
comparison study, run as a three-way experiment over evals/datasets/graph.jsonl:

- arm "rag":   the knowledge agent, articles only. The runbook articles (ADR-036) document
  every ONE-HOP fact in the graph, so this arm is not a strawman — answering a multi-hop
  question means retrieving and chaining several runbooks.
- arm "cte":   the incident agent with query_dependency_graph over the Postgres recursive CTE.
- arm "neo4j": the same agent and tool over the Cypher backend (settings.graph_backend flip).
  Skipped — reported, not failed — when Neo4j is unreachable: CI runs nightly without it.

Scoring is set overlap against ground truth: each case commits the exact impacted/dependency
set (computed from the seeded graph when the dataset was authored). The answer text is scanned
for CI names (closed universe, word-boundary match, team aliases like "sales team"), filtered
to the case's scope_types, queried entities excluded; precision/recall/F1 against `expected`.
Name-listing is demanded by every question, so extraction penalizes neither arm.

Also measured, LLM-free:
- tool-level CTE-vs-Cypher latency (agent latency is LLM-dominated and would drown a
  milliseconds-scale difference — the honest place to compare traversal cost is the tool);
- backend parity: both backends must return identical (name, depth) sets per case.

Hygiene: agent runs are sequential; created rows are snapshot-deleted (routing-suite pattern).
Floors (ADR-026): gated only if thresholds.toml has a [graph] section — added after the
measured baseline, report-only until then.
"""
# Implemented in M9.

from __future__ import annotations

import asyncio
import re
import time

from agents import Runner
from sqlalchemy import select

from app.agents.context import ChatContext
from app.agents.incident import incident_agent
from app.agents.knowledge import knowledge_agent
from app.config import get_settings
from app.db.database import SessionLocal
from app.db.models import CI, Order, Ticket, TicketComment
from evals.common import (
    DATASET_DIR,
    EVAL_USER,
    FLOORS,
    cost_latency_aggregates,
    load_jsonl,
    percentile,
    usage_fields,
)

LATENCY_REPS = 20  # tool-level reps per case query, per backend


def _universe() -> dict[str, str]:
    """All CI names -> ci_type: the closed extraction vocabulary."""
    with SessionLocal() as s:
        return {c.name: c.ci_type for c in s.scalars(select(CI))}


def _strip_citations(answer: str) -> str:
    """Remove citation artifacts before name extraction: runbook TITLES contain CI names
    ("Runbook: authentication stack (auth-service, sso-gateway, ldap-directory)"), so a rag
    answer's Sources block / inline [Runbook: …] markers would count as impact claims the
    agent never made. Applied to every arm uniformly (only citation-shaped spans match)."""
    answer = re.split(r"\bSources:", answer)[0]
    return re.sub(r"\[[^\]\[]*runbook[^\]\[]*\]", " ", answer, flags=re.IGNORECASE)


def _mentioned(answer: str, universe: dict[str, str]) -> set[str]:
    """CI names present in the answer. Word-boundary-ish so 'crm-db' never matches inside
    'crm-db-replica'; team nodes also match natural phrasing ('the sales team')."""
    text = _strip_citations(answer).lower()
    found = set()
    for name in universe:
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            found.add(name)
        elif name.startswith("team-"):
            org = name.removeprefix("team-")
            if re.search(rf"\b{org}\s+team\b|\bteam\s+{org}\b", text):
                found.add(name)
    return found


def _score(answer: str, case: dict, universe: dict[str, str]) -> dict:
    scope = set(case["scope_types"])
    expected = set(case["expected"])
    predicted = {
        n
        for n in _mentioned(answer, universe)
        if universe[n] in scope and n not in case["entities"]
    }
    tp = len(predicted & expected)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "exact": predicted == expected,
        "missed": sorted(expected - predicted),
        "extra": sorted(predicted - expected),
    }


async def _run_arm(arm: str, agent, cases: list[dict], universe: dict[str, str]) -> list[dict]:
    """One arm's 15 sequential agent runs, scored on the final answer text."""
    rows = []
    for case in cases:
        t0 = time.perf_counter()
        usage = {"cost_usd": None}
        try:
            result = await Runner.run(
                agent, case["question"], context=ChatContext(user_id=EVAL_USER)
            )
            answer = str(result.final_output)
            usage = usage_fields(result, get_settings().specialist_model)
            error = None
        except Exception as exc:  # noqa: BLE001 — a crashed run scores 0, like routing
            answer, error = "", f"{exc.__class__.__name__}: {exc}"
        rows.append(
            {
                "arm": arm,
                "question": case["question"],
                "entities": case["entities"],
                "direction": case["direction"],
                "hops": case["hops"],
                "n_expected": len(case["expected"]),
                **_score(answer, case, universe),
                # the knowledge agent's ADR-017 contract: refusals never carry "Sources:"
                "refused": arm == "rag" and "Sources:" not in answer,
                "answer_excerpt": answer[:800],  # evidence for triaging misses in --out JSON
                "error": error,
                "latency_s": round(time.perf_counter() - t0, 2),
                **usage,
            }
        )
    return rows


def _tool_latency(backend_query, cases: list[dict], ids: dict[str, str]) -> dict:
    """LLM-free traversal latency over every case query × LATENCY_REPS."""
    samples_ms: list[float] = []
    for _ in range(LATENCY_REPS):
        for case in cases:
            for entity in case["entities"]:
                t0 = time.perf_counter()
                backend_query(ids[entity], case["direction"], 10)
                samples_ms.append((time.perf_counter() - t0) * 1000)
    return {
        "n": len(samples_ms),
        "p50_ms": round(percentile(samples_ms, 50), 2),
        "p95_ms": round(percentile(samples_ms, 95), 2),
        "mean_ms": round(sum(samples_ms) / len(samples_ms), 2),
    }


def _parity(cases: list[dict], ids: dict[str, str]) -> list[str]:
    """LLM-free: both backends must return identical (name, depth) sets per case query."""
    from graph.neo4j_graph import query as neo_q
    from graph.postgres_graph import query as pg_q

    mismatches = []
    for case in cases:
        for entity in case["entities"]:
            pg = {(n["name"], n["depth"]) for n in pg_q(ids[entity], case["direction"], 10)}
            neo = {(n["name"], n["depth"]) for n in neo_q(ids[entity], case["direction"], 10)}
            if pg != neo:
                mismatches.append(f"{entity}/{case['direction']}: {sorted(pg ^ neo)}")
    return mismatches


def _arm_aggregates(rows: list[dict]) -> dict:
    n = len(rows)
    by_depth = {
        "shallow_1_2_hops": [r["f1"] for r in rows if r["hops"] <= 2],
        "deep_3plus_hops": [r["f1"] for r in rows if r["hops"] >= 3],
    }
    return {
        "f1_mean": round(sum(r["f1"] for r in rows) / n, 3),
        "precision_mean": round(sum(r["precision"] for r in rows) / n, 3),
        "recall_mean": round(sum(r["recall"] for r in rows) / n, 3),
        "exact_rate": round(sum(r["exact"] for r in rows) / n, 3),
        "f1_by_depth": {k: round(sum(v) / len(v), 3) if v else None for k, v in by_depth.items()},
        "refusals": sum(1 for r in rows if r.get("refused")),
        "errors": sum(1 for r in rows if r.get("error")),
        "n": n,
    }


def run_graph(**_ignored) -> dict:
    """Run the graph suite; retrieval-suite kwargs (k/threshold/refusal_mode) don't apply."""
    cases = load_jsonl(DATASET_DIR / "graph.jsonl")
    universe = _universe()
    with SessionLocal() as s:
        ids = {c.name: str(c.id) for c in s.scalars(select(CI))}

    from graph.neo4j_graph import ping as neo4j_ping

    neo4j_up = neo4j_ping()

    settings = get_settings()
    original_backend = settings.graph_backend

    # Snapshot: the incident agent may create tickets/comments while answering (routing-suite
    # hygiene — delete everything the runs created, keep the seeded DB pristine).
    with SessionLocal() as s:
        before_orders = set(s.scalars(select(Order.id)))
        before_tickets = set(s.scalars(select(Ticket.id)))
        before_comments = set(s.scalars(select(TicketComment.id)))

    rows: list[dict] = []
    try:
        settings.graph_backend = "postgres"
        rows += asyncio.run(_run_arm("rag", knowledge_agent, cases, universe))
        rows += asyncio.run(_run_arm("cte", incident_agent, cases, universe))
        if neo4j_up:
            settings.graph_backend = "neo4j"
            rows += asyncio.run(_run_arm("neo4j", incident_agent, cases, universe))
    finally:
        settings.graph_backend = original_backend
        with SessionLocal() as s:
            for c in s.scalars(
                select(TicketComment).where(TicketComment.id.notin_(before_comments))
            ):
                s.delete(c)
            for t in s.scalars(select(Ticket).where(Ticket.id.notin_(before_tickets))):
                s.delete(t)
            for o in s.scalars(select(Order).where(Order.id.notin_(before_orders))):
                s.delete(o)
            s.commit()

    # LLM-free instrumentation: traversal latency + backend parity.
    from graph.postgres_graph import query as pg_query

    tool_latency = {"postgres": _tool_latency(pg_query, cases, ids)}
    parity_mismatches = None
    if neo4j_up:
        from graph.neo4j_graph import query as neo_query

        tool_latency["neo4j"] = _tool_latency(neo_query, cases, ids)
        parity_mismatches = _parity(cases, ids)
    else:
        tool_latency["neo4j"] = None

    arms = {arm: [r for r in rows if r["arm"] == arm] for arm in ("rag", "cte", "neo4j")}
    aggregates = {
        arm: (_arm_aggregates(arm_rows) if arm_rows else None) for arm, arm_rows in arms.items()
    }
    report = {
        "suite": "graph",
        "neo4j_available": neo4j_up,
        "rows": rows,
        "aggregates": {
            "arms": aggregates,
            "tool_latency": tool_latency,
            "parity_mismatches": parity_mismatches,
            "n_cases": len(cases),
        },
    }
    report["cost_latency"] = cost_latency_aggregates(rows)

    floors = FLOORS.get("graph", {})
    passed = True
    if "cte_f1_mean" in floors and aggregates["cte"]:
        passed = aggregates["cte"]["f1_mean"] >= floors["cte_f1_mean"]
    if parity_mismatches:
        passed = False  # deterministic invariant, floor-free: the backends must agree
    report["passed"] = passed
    return report

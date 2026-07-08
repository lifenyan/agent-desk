"""Eval harness CLI: run the full suite (retrieval, routing, e2e, dedup, quality, graph,
slack) or --subset.

M1 implemented the retrieval suite, M2 routing; M4 plugged e2e (evals/suite_e2e.py) and dedup
(evals/suite_dedup.py) into the SUITES registry, moved the pass/fail floors into
evals/thresholds.toml (single source of truth, ADR-026 — floors are REGRESSION gates set
below observed variance, not perfection gates), and added:
- `--subset`: the deterministic cost-capped PR gate (ADR-026) = the FULL retrieval suite
  (~10 LLM calls — the answerable slice is LLM-free; M5 grew the refusal slice 5→10 so the
  refusal metric is a rate, not five coin flips) + the 10 routing cases flagged
  `"subset": true` in routing.jsonl (all 6 hard cases — they exist because they caught real
  bugs — plus one easy case per specialist and the ticket-update path). e2e, dedup, and
  quality are nightly-only. No random sampling: CI runs must be comparable run-to-run.
- a GitHub job-summary writer: when $GITHUB_STEP_SUMMARY is set (any Actions run), the
  per-suite metrics tables are appended there as markdown.
M5 added the quality suite (evals/suite_quality.py, LLM-as-judge — ADR-033), per-case
cost/latency on every suite (--out writes the full JSON; evals/results/baseline.json is a
committed run — ADR-034), and the routing wrong-handoff matrix. M9 added the graph suite
(evals/suite_graph.py — the plain-RAG vs Graph-RAG three-way comparison, ADR-036; nightly,
Neo4j arm self-skips when the optional server is absent). M8 added the slack suite
(evals/suite_slack.py — recorded thread fixtures through the real runner code incl. the
injection trap, ADR-039/041; nightly, no live Slack anywhere).

Scoring mirrors the two-stage refusal cascade (ADR-017):
- ANSWERABLE cases run DIRECTLY against rag.hybrid_search — no LLM in the loop (the only
  network call is embedding each query, cached after the first run): recall@5 + MRR at the
  article level, plus a false-refusal flag (would the deterministic stage-1 gate have wrongly
  suppressed the answer?).
- REFUSAL cases run through the knowledge agent (10 small LLM calls per run), because near-miss
  negative space ('email on smartwatch' vs the email-on-phone article) is measurably
  inseparable at the retrieval level — the agent reading the chunks IS the refusal mechanism
  under test. Detection is structural, keyed to the agent's output contract: a refusal offers
  a ticket and carries no "Sources:" list.

`--sweep` scores refusals at the RETRIEVAL level only (stage 1 alone, no LLM) across candidate
thresholds — the tuning evidence behind settings.retrieval_refusal_threshold (ADR-017).

The ROUTING suite (M2 — pulled forward from M4 because it needs the 3-specialist graph that
only now exists) runs every case through the ROUTER with real tools against the seeded DB
(~30 LLM runs; any rows the action agents create are deleted afterwards). Scored:
- routing accuracy: FIRST handoff target == expected_specialist;
- ping-pong: handoffs beyond the first per run (mean/max) — ADR-003's failure mode;
- handoff integrity (the concrete ADR-018 regression, found by hand in M1): every run must
  contain >= 1 handoff AND >= 1 real tool call and end with a non-empty answer — never
  "You're being transferred…" narration that ends the run;
- the multi-intent case must fire BOTH knowledge tools in a single run.
Use `--suite retrieval` alone for the cheap (LLM-free answerable path) tuning loop.
"""
# Retrieval suite implemented in M1; routing in M2; e2e + dedup + --subset + thresholds.toml
# + job summary in M4; quality + cost/latency + confusion matrix + --out in M5.

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agents import Runner  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agents.context import ChatContext  # noqa: E402
from app.agents.knowledge import knowledge_agent  # noqa: E402
from app.agents.router import router_agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.db.models import Order, Ticket, TicketComment  # noqa: E402
from app.rag.hybrid_search import hybrid_search, top_cosine  # noqa: E402
from evals.common import (  # noqa: E402
    DATASET_DIR,
    EVAL_USER,
    FLOORS,
    cost_latency_aggregates,
    load_jsonl as _load_jsonl,
    usage_fields,
)
from evals.metrics import dedupe_preserving_order, mrr, recall_at_k  # noqa: E402
from evals.suite_dedup import run_dedup  # noqa: E402
from evals.suite_e2e import run_e2e  # noqa: E402
from evals.suite_graph import run_graph  # noqa: E402
from evals.suite_quality import run_quality  # noqa: E402
from evals.suite_slack import run_slack  # noqa: E402

# Floors come from evals/thresholds.toml (ADR-026) — regression gates below observed variance.
RECALL_AT_5_FLOOR = FLOORS["retrieval"]["recall_at_5"]
REFUSAL_ACCURACY_FLOOR = FLOORS["retrieval"]["refusal_accuracy"]
FALSE_REFUSALS_MAX = FLOORS["retrieval"]["false_refusals_max"]
ROUTING_ACCURACY_FLOOR = FLOORS["routing"]["accuracy"]
ROUTING_INTEGRITY_FAILURES_MAX = FLOORS["routing"]["integrity_failures_max"]


def _agent_refused(answer: str) -> bool:
    """Structural refusal detector, anchored to the knowledge agent's output contract:
    refusals offer a ticket and never carry a "Sources:" citation list."""
    return "Sources:" not in answer and "ticket" in answer.lower()


async def _run_refusal_case(query: str) -> tuple[bool, str, dict]:
    result = await Runner.run(knowledge_agent, query, context=ChatContext())
    answer = str(result.final_output)
    return _agent_refused(answer), answer, usage_fields(result, get_settings().specialist_model)


def run_retrieval(
    k: int = 5, threshold: float | None = None, refusal_mode: str = "agent", **_ignored
) -> dict:
    """Run the retrieval suite; returns a report dict (printed by main, reused by M4 CI).
    Deliberately has no subset mode: at ~5 LLM calls it is already the cheap suite (ADR-026).

    refusal_mode: "agent" (default; the real cascade) or "retrieval" (stage 1 only, LLM-free —
    used by --sweep and anywhere an API key is unavailable).
    """
    settings = get_settings()
    threshold = threshold if threshold is not None else settings.retrieval_refusal_threshold
    cases = _load_jsonl(DATASET_DIR / "retrieval.jsonl")

    rows = []
    with SessionLocal() as session:
        for case in cases:
            t0 = time.perf_counter()
            results = hybrid_search(session, case["query"], top_k=k)
            article_ids = dedupe_preserving_order([str(r.article_id) for r in results])
            best = top_cosine(results)
            gate_refuses = best < threshold
            if case.get("refusal"):
                # LLM-free by default: cost fields only exist on the agent-mode path.
                usage = {"cost_usd": None}
                if refusal_mode == "agent":
                    refused, _, usage = asyncio.run(_run_refusal_case(case["query"]))
                else:
                    refused = gate_refuses
                rows.append(
                    {
                        "query": case["query"],
                        "kind": f"refusal/{refusal_mode}",
                        "recall": None,
                        "mrr": None,
                        "top_cosine": best,
                        "pass": refused,
                        "latency_s": round(time.perf_counter() - t0, 2),
                        **usage,
                    }
                )
            else:
                expected = case["expected_article_ids"]
                recall = recall_at_k(expected, article_ids, k=k)
                rows.append(
                    {
                        "query": case["query"],
                        "kind": "answerable",
                        "recall": recall,
                        "mrr": mrr(expected, article_ids),
                        "top_cosine": best,
                        # an answerable query must also CLEAR the stage-1 gate: full recall is
                        # useless if the deterministic gate would have suppressed the answer
                        "pass": recall == 1.0 and not gate_refuses,
                        "false_refusal": gate_refuses,
                        # LLM-free: only the (cached-after-first-run) query embedding bills,
                        # far below a rounding digit — latency is the honest number here.
                        "latency_s": round(time.perf_counter() - t0, 2),
                        "cost_usd": None,
                    }
                )

    answerable = [r for r in rows if r["kind"] == "answerable"]
    refusals = [r for r in rows if r["kind"].startswith("refusal")]
    report = {
        "suite": "retrieval",
        "k": k,
        "threshold": threshold,
        "refusal_mode": refusal_mode,
        "rows": rows,
        "aggregates": {
            "recall_at_k": sum(r["recall"] for r in answerable) / len(answerable),
            "mrr": sum(r["mrr"] for r in answerable) / len(answerable),
            "false_refusals": sum(1 for r in answerable if r["false_refusal"]),
            "refusal_accuracy": (
                sum(1 for r in refusals if r["pass"]) / len(refusals) if refusals else 1.0
            ),
            "n_answerable": len(answerable),
            "n_refusal": len(refusals),
        },
    }
    report["cost_latency"] = cost_latency_aggregates(rows)
    report["passed"] = (
        report["aggregates"]["recall_at_k"] >= RECALL_AT_5_FLOOR
        and report["aggregates"]["refusal_accuracy"] >= REFUSAL_ACCURACY_FLOOR
        and report["aggregates"]["false_refusals"] <= FALSE_REFUSALS_MAX
    )
    return report


# ---------------------------------------------------------------------------------------------
# Routing suite (M2; pulled forward from M4). Runs THROUGH the router — the M1 evals' blind
# spot was exactly that they called knowledge_agent directly and never exercised the handoff
# (ADR-018 was found by hand). Needs a live model, which is why this is an eval, not a test.
# ---------------------------------------------------------------------------------------------


def _run_trace(result) -> tuple[list[str], list[str]]:
    """(handoff targets, tool names) from a run, in order — same detection as the ADR-018 fix."""
    handoffs, tools = [], []
    for item in result.new_items:
        if item.type == "handoff_output_item":
            handoffs.append(item.target_agent.name)
        elif item.type == "tool_call_item":
            tools.append(getattr(item.raw_item, "name", "?"))
    return handoffs, tools


async def _routing_case(case: dict) -> dict:
    t0 = time.perf_counter()
    try:
        result = await Runner.run(
            router_agent, case["query"], context=ChatContext(user_id=EVAL_USER)
        )
    except Exception as exc:  # noqa: BLE001 — a crashed run is an integrity failure, not a crash
        return {
            "query": case["query"],
            "expected": case["expected_specialist"],
            "routed_to": f"<error: {exc.__class__.__name__}>",
            "hard": case.get("hard", False),
            "correct": False,
            "ping_pong": 0,
            "integrity_ok": False,
            "tools": [],
            "latency_s": round(time.perf_counter() - t0, 2),
            "cost_usd": None,
        }
    handoffs, tools = _run_trace(result)
    return {
        "query": case["query"],
        "expected": case["expected_specialist"],
        "routed_to": handoffs[0] if handoffs else "<none>",
        "hard": case.get("hard", False),
        "correct": bool(handoffs) and handoffs[0] == case["expected_specialist"],
        # hops beyond the first handoff = router→A→router→B churn (ADR-003)
        "ping_pong": max(0, len(handoffs) - 1),
        # the ADR-018 non-answer: no handoff, or zero tool calls, or an empty final output
        "integrity_ok": bool(handoffs) and bool(tools) and bool(str(result.final_output).strip()),
        "tools": tools,
        "latency_s": round(time.perf_counter() - t0, 2),
        **usage_fields(result, get_settings().specialist_model),
    }


def run_routing(subset: bool = False, **_ignored) -> dict:
    """Run the routing suite; retrieval-suite kwargs (k/threshold/refusal_mode) don't apply.

    subset=True keeps only the cases flagged `"subset": true` in routing.jsonl — the
    deterministic PR gate (ADR-026): all 6 hard cases + one easy case per specialist + the
    ticket-update path. Selection lives in the dataset, so it never depends on run order.
    """
    cases = _load_jsonl(DATASET_DIR / "routing.jsonl")
    if subset:
        cases = [c for c in cases if c.get("subset")]

    # Action agents write to the seeded DB during eval runs; snapshot + delete keeps it pristine.
    with SessionLocal() as s:
        before_orders = set(s.scalars(select(Order.id)))
        before_tickets = set(s.scalars(select(Ticket.id)))
        before_comments = set(s.scalars(select(TicketComment.id)))

    async def _all() -> list[dict]:
        return [await _routing_case(case) for case in cases]

    try:
        rows = asyncio.run(_all())
    finally:
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

    for case, row in zip(cases, rows):
        if case.get("expects_tools"):
            row["multi_intent_ok"] = set(case["expects_tools"]) <= set(row["tools"])

    hard = [r for r in rows if r["hard"]]
    multi = [r for r in rows if "multi_intent_ok" in r]
    ping = [r["ping_pong"] for r in rows]

    # Wrong-handoff matrix (M5): expected specialist -> where runs actually went, counted over
    # every case. The accuracy number says HOW OFTEN routing fails; this says WHERE it leaks —
    # the input for fixing router instructions (a systematic knowledge->incident drift reads
    # very differently from scattered one-offs).
    confusion: dict[str, dict[str, int]] = {}
    for r in rows:
        row = confusion.setdefault(r["expected"], {})
        row[r["routed_to"]] = row.get(r["routed_to"], 0) + 1

    report = {
        "suite": "routing",
        "rows": rows,
        "aggregates": {
            "accuracy": sum(r["correct"] for r in rows) / len(rows),
            "hard_accuracy": (sum(r["correct"] for r in hard) / len(hard)) if hard else None,
            "ping_pong_mean": sum(ping) / len(ping),
            "ping_pong_max": max(ping),
            "integrity_failures": sum(1 for r in rows if not r["integrity_ok"]),
            "multi_intent_ok": all(r["multi_intent_ok"] for r in multi) if multi else None,
            "confusion": confusion,
            "n": len(rows),
        },
    }
    report["cost_latency"] = cost_latency_aggregates(rows)
    agg = report["aggregates"]
    report["subset"] = subset
    report["passed"] = (
        agg["accuracy"] >= ROUTING_ACCURACY_FLOOR
        and agg["integrity_failures"] <= ROUTING_INTEGRITY_FAILURES_MAX
        and agg["multi_intent_ok"] is not False
    )
    return report


# e2e (side-effect assertions through the live HTTP API, ADR-027), dedup (the ADR-021
# gray-band judgment eval, ADR-028), quality (LLM-as-judge, ADR-033), graph (the M9
# three-way RAG-vs-Graph-RAG comparison, ADR-036), and slack (M8 recorded-fixture ingestion
# incl. the injection trap, ADR-039/041) live in their own modules; all five are
# nightly-only suites.
SUITES = {
    "retrieval": run_retrieval,
    "routing": run_routing,
    "e2e": run_e2e,
    "dedup": run_dedup,
    "quality": run_quality,
    "graph": run_graph,
    "slack": run_slack,
}
SUBSET_SUITES = ("retrieval", "routing")  # the PR gate: the rest stay nightly (ADR-026)


def _print_cost_latency(report: dict) -> None:
    """One line per suite: what it cost and how long cases took (M5; totals + p50/p95)."""
    cl = report.get("cost_latency")
    if not cl or cl["total_latency_s"] is None:
        return
    cost = (
        f"${cl['total_cost_usd']:.4f} over {cl['cases_with_cost']} billed cases"
        if cl["total_cost_usd"] is not None
        else "n/a (no SDK usage on this path)"
    )
    print(
        f"cost: {cost} | case latency p50/p95: {cl['latency_p50_s']}s/{cl['latency_p95_s']}s | "
        f"case wall time total: {cl['total_latency_s']}s"
    )


def _print_confusion(confusion: dict[str, dict[str, int]]) -> None:
    """Wrong-handoff matrix: rows = expected specialist, columns = where runs actually went."""
    targets = sorted({t for row in confusion.values() for t in row})
    print("wrong-handoff matrix (expected \\ routed to):")
    header = f"{'':<14}" + "".join(f"{t:>14}" for t in targets)
    print(header)
    for expected in sorted(confusion):
        cells = "".join(f"{confusion[expected].get(t, 0):>14}" for t in targets)
        print(f"{expected:<14}{cells}")


def _print_retrieval_report(report: dict) -> None:
    print(
        f"\n=== retrieval suite (k={report['k']}, stage-1 threshold={report['threshold']}, "
        f"refusal mode={report['refusal_mode']}) ==="
    )
    header = f"{'query':<48} {'kind':<16} {'recall@k':>8} {'mrr':>6} {'top_cos':>8}  result"
    print(header)
    print("-" * len(header))
    for r in report["rows"]:
        recall = f"{r['recall']:.2f}" if r["recall"] is not None else "—"
        rr = f"{r['mrr']:.2f}" if r["mrr"] is not None else "—"
        flag = "PASS" if r["pass"] else "FAIL"
        if r.get("false_refusal"):
            flag += " (false refusal)"
        print(
            f"{r['query'][:48]:<48} {r['kind']:<16} {recall:>8} {rr:>6} "
            f"{r['top_cosine']:>8.3f}  {flag}"
        )
    agg = report["aggregates"]
    print("-" * len(header))
    print(
        f"recall@{report['k']}: {agg['recall_at_k']:.3f} (floor {RECALL_AT_5_FLOOR}) | "
        f"MRR: {agg['mrr']:.3f} | "
        f"refusals: {agg['refusal_accuracy'] * agg['n_refusal']:.0f}/{agg['n_refusal']} | "
        f"false refusals: {agg['false_refusals']} | "
        f"suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_cost_latency(report)


def _print_routing_report(report: dict) -> None:
    print("\n=== routing suite (through the router, live model) ===")
    header = f"{'query':<52} {'expected':<12} {'routed to':<12} {'pp':>3} {'ok':>3}  result"
    print(header)
    print("-" * len(header))
    for r in report["rows"]:
        flag = "PASS" if r["correct"] and r["integrity_ok"] else "FAIL"
        if r["hard"]:
            flag += " (hard)"
        if r.get("multi_intent_ok") is False:
            flag += " (multi-intent tools missing)"
        print(
            f"{r['query'][:52]:<52} {r['expected']:<12} {r['routed_to']:<12} "
            f"{r['ping_pong']:>3} {'y' if r['integrity_ok'] else 'N':>3}  {flag}"
        )
    agg = report["aggregates"]
    print("-" * len(header))
    hard_acc = f"{agg['hard_accuracy']:.3f}" if agg["hard_accuracy"] is not None else "—"
    print(
        f"accuracy: {agg['accuracy']:.3f} (floor {ROUTING_ACCURACY_FLOOR}) | hard: {hard_acc} | "
        f"ping-pong mean/max: {agg['ping_pong_mean']:.2f}/{agg['ping_pong_max']} | "
        f"integrity failures: {agg['integrity_failures']} | "
        f"multi-intent: {agg['multi_intent_ok']} | "
        f"suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_confusion(agg["confusion"])
    _print_cost_latency(report)


def _print_e2e_report(report: dict) -> None:
    print("\n=== e2e suite (side effects through the live HTTP API) ===")
    header = f"{'flow':<18} {'result':<6}  detail"
    print(header)
    print("-" * 100)
    for r in report["rows"]:
        print(f"{r['flow']:<18} {'PASS' if r['ok'] else 'FAIL':<6}  {r['detail']}")
    agg = report["aggregates"]
    print("-" * 100)
    print(
        f"flows: {agg['flows_passed']}/{agg['n']} "
        f"(floor {FLOORS['e2e']['flow_pass_rate']:.2f} pass rate) | "
        f"suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_cost_latency(report)


def _print_dedup_report(report: dict) -> None:
    print("\n=== dedup suite (ADR-021 gray-band judgment, incident agent with real tools) ===")
    header = f"{'report':<56} {'kind':<5} {'action':<8} {'top_sim':>7}  result"
    print(header)
    print("-" * len(header))
    for r in report["rows"]:
        sim = f"{r['top_similarity']:.3f}" if r["top_similarity"] is not None else "—"
        flag = "PASS" if r["ok"] else "FAIL"
        if r["kind"] == "link" and r["action"] == "linked" and not r["ok"]:
            flag += " (linked to wrong group)"
        print(f"{r['report'][:56]:<56} {r['kind']:<5} {r['action']:<8} {sim:>7}  {flag}")
    agg = report["aggregates"]
    print("-" * len(header))
    link = f"{agg['link_accuracy']:.3f}" if agg["link_accuracy"] is not None else "—"
    trap = f"{agg['trap_accuracy']:.3f}" if agg["trap_accuracy"] is not None else "—"
    print(
        f"link accuracy: {link} ({agg['n_link']} probes) | "
        f"trap accuracy: {trap} ({agg['n_trap']} traps) | "
        f"overall: {agg['accuracy']:.3f} (floor {FLOORS['dedup']['accuracy']}) | "
        f"suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_cost_latency(report)


def _print_quality_report(report: dict) -> None:
    print(f"\n=== quality suite (LLM-as-judge: {report['judge_model']}, ADR-033) ===")
    header = f"{'query':<52} {'faith':>5} {'help':>5}  weakest justification"
    print(header)
    print("-" * 110)
    for r in report["rows"]:
        # Show the justification for whichever dimension scored lower — that's the one a
        # reader triaging a red nightly wants first.
        just = (
            r["faithfulness_justification"]
            if r["faithfulness"] <= r["helpfulness"]
            else r["helpfulness_justification"]
        )
        print(f"{r['query'][:52]:<52} {r['faithfulness']:>5} {r['helpfulness']:>5}  {just[:44]}")
    agg = report["aggregates"]
    print("-" * 110)
    floors_note = "report-only, no floor yet (ADR-026: floors need a baseline + variance)"
    if FLOORS.get("quality"):
        floors_note = f"floors: {FLOORS['quality']}"
    print(
        f"faithfulness mean: {agg['faithfulness_mean']} {agg['faithfulness_distribution']} | "
        f"helpfulness mean: {agg['helpfulness_mean']} {agg['helpfulness_distribution']} | "
        f"{floors_note} | suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_cost_latency(report)


def _print_slack_report(report: dict) -> None:
    print("\n=== slack suite (recorded thread fixtures through the runner + live API — M8) ===")
    header = f"{'case':<18} {'result':<6}  detail"
    print(header)
    print("-" * 110)
    for r in report["rows"]:
        print(f"{r['case']:<18} {'PASS' if r['ok'] else 'FAIL':<6}  {r['detail'][:100]}")
    agg = report["aggregates"]
    print("-" * 110)
    floor_note = (
        f"floor {report['floor']:.2f} pass rate"
        if report.get("floor") is not None
        else "report-only, no floor yet (ADR-026: floors need a baseline)"
    )
    print(
        f"cases: {agg['cases_passed']}/{agg['n']} ({floor_note}) | "
        f"suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_cost_latency(report)


def _print_graph_report(report: dict) -> None:
    print("\n=== graph suite (plain RAG vs Graph-RAG on multi-hop questions — ADR-036) ===")
    header = (
        f"{'entities':<36} {'dir':<12} {'hops':>4} {'arm':<6} {'f1':>5} {'exact':>5}  worst misses"
    )
    print(header)
    print("-" * len(header))
    for r in report["rows"]:
        missed = ", ".join(r["missed"][:3]) + ("…" if len(r["missed"]) > 3 else "")
        flag = "refused" if r.get("refused") else (r.get("error") or "")
        print(
            f"{','.join(r['entities'])[:36]:<36} {r['direction']:<12} {r['hops']:>4} "
            f"{r['arm']:<6} {r['f1']:>5.2f} {'y' if r['exact'] else 'N':>5}  "
            f"{(flag + ' ' if flag else '') + missed}"
        )
    agg = report["aggregates"]
    print("-" * len(header))
    for arm, a in agg["arms"].items():
        if a is None:
            print(f"{arm:>6}: skipped (Neo4j unreachable — postgres path unaffected)")
            continue
        print(
            f"{arm:>6}: F1 {a['f1_mean']:.3f} (P {a['precision_mean']:.3f} / R "
            f"{a['recall_mean']:.3f}) | exact {a['exact_rate']:.3f} | by depth: "
            f"1-2 hops {a['f1_by_depth']['shallow_1_2_hops']} / 3+ hops "
            f"{a['f1_by_depth']['deep_3plus_hops']} | refusals {a['refusals']} | "
            f"errors {a['errors']}"
        )
    lat = agg["tool_latency"]
    neo_lat = (
        f"neo4j p50/p95 {lat['neo4j']['p50_ms']}/{lat['neo4j']['p95_ms']} ms"
        if lat["neo4j"]
        else "neo4j n/a"
    )
    parity = (
        "n/a"
        if agg["parity_mismatches"] is None
        else ("OK" if not agg["parity_mismatches"] else f"MISMATCH {agg['parity_mismatches']}")
    )
    print(
        f"tool latency ({LATENCY_REPS_NOTE}): postgres p50/p95 "
        f"{lat['postgres']['p50_ms']}/{lat['postgres']['p95_ms']} ms | {neo_lat} | "
        f"backend parity: {parity} | suite: {'PASS' if report['passed'] else 'FAIL'}"
    )
    _print_cost_latency(report)


LATENCY_REPS_NOTE = "LLM-free, tool-level"

_PRINTERS = {
    "retrieval": _print_retrieval_report,
    "routing": _print_routing_report,
    "e2e": _print_e2e_report,
    "dedup": _print_dedup_report,
    "quality": _print_quality_report,
    "graph": _print_graph_report,
    "slack": _print_slack_report,
}


# --- GitHub job summary (M4): markdown metrics tables for $GITHUB_STEP_SUMMARY ---------------


def _summary_rows(report: dict) -> list[tuple[str, str, str, str]]:
    """(suite, metric, value, floor) rows for the markdown summary table."""
    agg = report["aggregates"]
    if report["suite"] == "retrieval":
        return [
            ("retrieval", "recall@5", f"{agg['recall_at_k']:.3f}", f"≥ {RECALL_AT_5_FLOOR}"),
            ("retrieval", "MRR", f"{agg['mrr']:.3f}", "—"),
            (
                "retrieval",
                "refusal accuracy",
                f"{agg['refusal_accuracy']:.3f}",
                f"≥ {REFUSAL_ACCURACY_FLOOR}",
            ),
            ("retrieval", "false refusals", str(agg["false_refusals"]), f"≤ {FALSE_REFUSALS_MAX}"),
        ]
    if report["suite"] == "routing":
        n = f" (subset, n={agg['n']})" if report.get("subset") else f" (n={agg['n']})"
        hard = f"{agg['hard_accuracy']:.3f}" if agg["hard_accuracy"] is not None else "—"
        return [
            ("routing" + n, "accuracy", f"{agg['accuracy']:.3f}", f"≥ {ROUTING_ACCURACY_FLOOR}"),
            ("routing" + n, "hard-case accuracy", hard, "—"),
            (
                "routing" + n,
                "ping-pong mean/max",
                f"{agg['ping_pong_mean']:.2f}/{agg['ping_pong_max']}",
                "—",
            ),
            (
                "routing" + n,
                "integrity failures",
                str(agg["integrity_failures"]),
                f"≤ {ROUTING_INTEGRITY_FAILURES_MAX}",
            ),
        ]
    if report["suite"] == "e2e":
        rows = [
            (
                "e2e",
                "flows passed",
                f"{agg['flows_passed']}/{agg['n']}",
                f"≥ {FLOORS['e2e']['flow_pass_rate']:.2f} rate",
            )
        ]
        rows += [("e2e", r["flow"], "PASS" if r["ok"] else "FAIL", "—") for r in report["rows"]]
        return rows
    if report["suite"] == "slack":
        floor = (
            f"≥ {report['floor']:.2f} rate" if report.get("floor") is not None else "report-only"
        )
        rows = [("slack", "cases passed", f"{agg['cases_passed']}/{agg['n']}", floor)]
        rows += [("slack", r["case"], "PASS" if r["ok"] else "FAIL", "—") for r in report["rows"]]
        return rows
    if report["suite"] == "dedup":
        link = f"{agg['link_accuracy']:.3f}" if agg["link_accuracy"] is not None else "—"
        trap = f"{agg['trap_accuracy']:.3f}" if agg["trap_accuracy"] is not None else "—"
        return [
            (
                "dedup",
                "gray-band accuracy",
                f"{agg['accuracy']:.3f}",
                f"≥ {FLOORS['dedup']['accuracy']}",
            ),
            ("dedup", "link accuracy", link, "—"),
            ("dedup", "trap accuracy", trap, "—"),
        ]
    if report["suite"] == "graph":
        floors = FLOORS.get("graph", {})
        cte_floor = f"≥ {floors['cte_f1_mean']}" if "cte_f1_mean" in floors else "report-only"
        rows = []
        for arm, a in agg["arms"].items():
            if a is None:
                rows.append(("graph", f"{arm} arm", "skipped (Neo4j unreachable)", "—"))
                continue
            rows.append(
                (
                    "graph",
                    f"{arm} F1 (exact-set rate)",
                    f"{a['f1_mean']:.3f} ({a['exact_rate']:.3f})",
                    cte_floor if arm == "cte" else "—",
                )
            )
        lat = agg["tool_latency"]
        neo = f"{lat['neo4j']['p50_ms']}ms" if lat["neo4j"] else "n/a"
        rows.append(
            (
                "graph",
                "tool latency p50 (pg / neo4j)",
                f"{lat['postgres']['p50_ms']}ms / {neo}",
                "—",
            )
        )
        if agg["parity_mismatches"] is not None:
            rows.append(
                (
                    "graph",
                    "backend parity",
                    "OK" if not agg["parity_mismatches"] else "MISMATCH",
                    "exact",
                )
            )
        return rows
    if report["suite"] == "quality":
        floors = FLOORS.get("quality", {})
        return [
            (
                "quality",
                "faithfulness mean (1–5)",
                f"{agg['faithfulness_mean']}",
                f"≥ {floors['faithfulness_mean']}"
                if "faithfulness_mean" in floors
                else "report-only",
            ),
            (
                "quality",
                "helpfulness mean (1–5)",
                f"{agg['helpfulness_mean']}",
                f"≥ {floors['helpfulness_mean']}"
                if "helpfulness_mean" in floors
                else "report-only",
            ),
        ]
    return []


def _cost_summary_row(report: dict) -> tuple[str, str, str, str] | None:
    """Cost/latency line for the markdown summary (M5) — the M6 Langfuse cross-check number."""
    cl = report.get("cost_latency")
    if not cl or cl["total_latency_s"] is None:
        return None
    cost = f"${cl['total_cost_usd']:.4f}" if cl["total_cost_usd"] is not None else "n/a"
    return (
        report["suite"],
        "cost / latency p50/p95",
        f"{cost} · {cl['latency_p50_s']}s/{cl['latency_p95_s']}s",
        "—",
    )


def _write_github_summary(reports: list[dict]) -> None:
    """Append the metrics table to the GitHub Actions job summary, if we are in one."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Eval metrics",
        "",
        "| suite | metric | value | floor |",
        "|---|---|---|---|",
    ]
    for report in reports:
        for suite, metric, value, floor in _summary_rows(report):
            lines.append(f"| {suite} | {metric} | {value} | {floor} |")
        cost_row = _cost_summary_row(report)
        if cost_row:
            lines.append("| {} | {} | {} | {} |".format(*cost_row))
    lines.append("")
    verdict = (
        "✅ all suites passed"
        if all(r["passed"] for r in reports)
        else "❌ FAILED: " + ", ".join(r["suite"] for r in reports if not r["passed"])
    )
    lines += [verdict, ""]
    with open(path, "a") as f:
        f.write("\n".join(lines))


def _sweep(lo: float, hi: float, step: float) -> None:
    """Stage-1 threshold sweep (LLM-free): the tuning evidence for ADR-017."""
    t = lo
    print(f"\n{'threshold':>9} {'refusals ok':>12} {'false refusals':>15}")
    while t <= hi + 1e-9:
        report = run_retrieval(threshold=round(t, 3), refusal_mode="retrieval")
        agg = report["aggregates"]
        print(
            f"{t:>9.3f} {agg['refusal_accuracy'] * agg['n_refusal']:>10.0f}/{agg['n_refusal']} "
            f"{agg['false_refusals']:>15}"
        )
        t += step


def _write_json(reports: list[dict], path: str, wall_time_s: float) -> None:
    """Full machine-readable results (M5): per-case rows incl. cost/latency, aggregates, and
    the models everything binds to. evals/results/baseline.json is a committed run of this."""
    settings = get_settings()
    payload = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "models": {
            "specialist": settings.specialist_model,
            "triage": settings.triage_model,
            "embedding": settings.embedding_model,
            "judge": settings.judge_model,
        },
        "wall_time_s": round(wall_time_s, 1),
        "total_cost_usd": round(
            sum(
                r["cost_latency"]["total_cost_usd"]
                for r in reports
                if r.get("cost_latency") and r["cost_latency"]["total_cost_usd"] is not None
            ),
            4,
        ),
        "passed": all(r["passed"] for r in reports),
        "reports": reports,
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nresults written to {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, action="append", help="default: all suites")
    parser.add_argument(
        "--subset",
        action="store_true",
        help="cost-capped PR gate (ADR-026): full retrieval + the 10 flagged routing cases; "
        "the other suites are skipped (nightly-only). Overrides --suite.",
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--threshold", type=float, help="override stage-1 threshold (tuning)")
    parser.add_argument(
        "--refusal-mode",
        choices=["agent", "retrieval"],
        default="agent",
        help="score refusal cases via the knowledge agent (default) or stage 1 only (LLM-free)",
    )
    parser.add_argument("--sweep", nargs=3, type=float, metavar=("LO", "HI", "STEP"))
    parser.add_argument(
        "--out", metavar="PATH", help="write full JSON results (per-case cost/latency) here"
    )
    args = parser.parse_args()

    if args.sweep:
        _sweep(*args.sweep)
        return 0

    suite_names = list(SUBSET_SUITES) if args.subset else (args.suite or list(SUITES))
    ok = True
    reports = []
    t_start = time.perf_counter()
    for name in suite_names:
        report = SUITES[name](
            k=args.k,
            threshold=args.threshold,
            refusal_mode=args.refusal_mode,
            subset=args.subset,
        )
        _PRINTERS[report["suite"]](report)
        reports.append(report)
        ok = ok and report["passed"]

    if args.subset:
        routing_n = next((r["aggregates"]["n"] for r in reports if r["suite"] == "routing"), 0)
        refusal_n = next(
            (r["aggregates"]["n_refusal"] for r in reports if r["suite"] == "retrieval"), 0
        )
        measured = sum(
            r["cost_latency"]["total_cost_usd"]
            for r in reports
            if r.get("cost_latency") and r["cost_latency"]["total_cost_usd"] is not None
        )
        print(
            f"\nsubset cost: {routing_n} routing agent runs + {refusal_n} refusal agent runs "
            f"(gpt-5-mini) + ~40 query embeddings — measured ${measured:.4f} this run"
        )

    _write_github_summary(reports)
    if args.out:
        _write_json(reports, args.out, time.perf_counter() - t_start)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

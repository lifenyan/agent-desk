"""Caching A/B report (M6, requirement 4): compare a caches-ON eval run against a caches-OFF run.

The two arms are produced by the SAME command, differing only in the deliberate OFF flag
(ADR-044 — `CACHES_DISABLED=1`, not a simulated Redis outage):

    python -m evals.run_evals --suite retrieval --suite e2e --out ignore/tem/m6_ab_on.json
    CACHES_DISABLED=1 python -m evals.run_evals --suite retrieval --suite e2e \
        --out ignore/tem/m6_ab_off.json

then:  python scripts/ab_caching_report.py ignore/tem/m6_ab_on.json ignore/tem/m6_ab_off.json

What it compares, per the honest-caveat rule (report per-flow, never one blended number —
the semantic cache only fires on paraphrase repeats, so the win concentrates in the
knowledge_cache flow and a blended mean would bury both the win and its narrowness):
  - per-e2e-flow wall-clock latency ON vs OFF (the harness measured it on both arms)
  - retrieval-suite latency aggregates (the embedding cache's arena)
  - per-arm LLM cost where the harness could meter it (suite totals) — e2e flows bill inside
    the suite-spawned server (ADR-034), so their cost delta comes from Langfuse traces, which
    both arms' servers emitted; pass --langfuse-cost to sum each arm's chat traces by the
    run's time window (requires keys and that nothing else hit the API meanwhile).
"""
# Implemented in M6 (ADR-044).

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _suite(payload: dict, name: str) -> dict | None:
    return next((r for r in payload["reports"] if r["suite"] == name), None)


def _pct_delta(on: float | None, off: float | None) -> str:
    if not on or not off:
        return "—"
    return f"{(off - on) / off:+.0%} vs OFF" if off else "—"


def _langfuse_window_cost(payload: dict) -> float | None:
    """Sum Langfuse 'chat' trace costs over this run's wall-time window (± slack)."""
    from app.observability import tracing

    client = tracing.get_langfuse()
    if client is None:
        return None
    end = datetime.datetime.fromisoformat(payload["generated_at"])
    start = end - datetime.timedelta(seconds=payload["wall_time_s"] + 120)
    total, page = 0.0, 1
    while True:
        resp = client.api.trace.list(
            name="chat", from_timestamp=start, to_timestamp=end, page=page, limit=100
        )
        total += sum(t.total_cost or 0.0 for t in resp.data)
        if page >= resp.meta.total_pages:
            break
        page += 1
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("on_json", type=Path, help="--out JSON of the caches-ON arm")
    parser.add_argument("off_json", type=Path, help="--out JSON of the caches-OFF arm")
    parser.add_argument(
        "--langfuse-cost",
        action="store_true",
        help="also sum each arm's chat-trace cost from Langfuse (closes the e2e cost gap)",
    )
    args = parser.parse_args()

    on = json.loads(args.on_json.read_text())
    off = json.loads(args.off_json.read_text())

    print("\n## Caching A/B — caches ON vs CACHES_DISABLED=1 (ADR-044)\n")

    e2e_on, e2e_off = _suite(on, "e2e"), _suite(off, "e2e")
    if e2e_on and e2e_off:
        off_rows = {r["flow"]: r for r in e2e_off["rows"]}
        print("| e2e flow | ON latency | OFF latency | latency saved | ON ok | OFF ok |")
        print("|---|---|---|---|---|---|")
        for r in e2e_on["rows"]:
            o = off_rows.get(r["flow"])
            if o is None:
                continue
            print(
                f"| {r['flow']} | {r['latency_s']:.1f}s | {o['latency_s']:.1f}s "
                f"| {_pct_delta(r['latency_s'], o['latency_s'])} "
                f"| {'PASS' if r['ok'] else 'FAIL'} | {'PASS' if o['ok'] else 'FAIL'} |"
            )
        con, coff = e2e_on["cost_latency"], e2e_off["cost_latency"]
        print(
            f"\ne2e case latency p50: ON {con['latency_p50_s']}s vs OFF {coff['latency_p50_s']}s "
            f"({_pct_delta(con['latency_p50_s'], coff['latency_p50_s'])}); "
            f"p95: ON {con['latency_p95_s']}s vs OFF {coff['latency_p95_s']}s"
        )

    ret_on, ret_off = _suite(on, "retrieval"), _suite(off, "retrieval")
    if ret_on and ret_off:
        con, coff = ret_on["cost_latency"], ret_off["cost_latency"]
        print(
            f"retrieval case latency p50: ON {con['latency_p50_s']}s vs OFF "
            f"{coff['latency_p50_s']}s ({_pct_delta(con['latency_p50_s'], coff['latency_p50_s'])}); "
            f"p95: ON {con['latency_p95_s']}s vs OFF {coff['latency_p95_s']}s"
        )
        print(
            f"retrieval metered cost: ON ${con['total_cost_usd'] or 0:.4f} vs OFF "
            f"${coff['total_cost_usd'] or 0:.4f}"
        )

    print(
        f"harness-metered total: ON ${on['total_cost_usd']:.4f} vs OFF ${off['total_cost_usd']:.4f}"
        " (e2e agent spend bills inside the spawned server — see below / ADR-034)"
    )

    if args.langfuse_cost:
        lc_on, lc_off = _langfuse_window_cost(on), _langfuse_window_cost(off)
        if lc_on is not None and lc_off is not None:
            saved = _pct_delta(lc_on, lc_off)
            print(
                f"Langfuse chat-trace cost (covers the e2e server): ON ${lc_on:.4f} vs "
                f"OFF ${lc_off:.4f} ({saved})"
            )
        else:
            print("Langfuse cost: keys not configured — skipped")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

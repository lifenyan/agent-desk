"""E2E suite (M4, ADR-027): the M2/M3 acceptance scripts formalized, nightly + on demand.

Runs every flow through the REAL HTTP contract (POST /chat, /approvals) against a live API —
by default a uvicorn this module spawns itself on a dedicated port (8123, never :8000: M2's
acceptance was briefly swallowed by a stale dev server squatting there; a dedicated port makes
that class of accident impossible). Set E2E_API_URL to target an already-running API instead.

What makes this suite different from tests/ and the other eval suites:
- it asserts SIDE EFFECTS, not answers: order rows reaching the right state for the RIGHT user
  (the user_tools DESIGN NOTE debt — "assert identity/ownership in the M4 e2e eval"),
  dedup linking vs creating, embeddings present at creation;
- it goes through routes_chat, so it exercises the M3 semantic cache — deliberately: the
  knowledge flow asserts a fresh-session paraphrase is served cached=true (the M3 contract is
  part of the product), and the refusal flow asserts refusals are never stored. semcache:* is
  flushed in setup so every run starts from the same cache state (precedent:
  ignore/tem/m3_semantic_cache_demo.py part B); fresh uuid4 session_ids per request keep the
  first-turn-only session policy satisfied.
- approvals are driven from a FRESH httpx client after the chat run ended — the "another
  process" half of the ADR-020 HITL contract (state lives in the DB, not the run).

Cleanup mirrors the routing suite: snapshot ids before, delete everything new after — the
seeded DB (plus the intentional M2 demo rows) is left exactly as found.
"""
# Implemented in M4.

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx
from sqlalchemy import select

from app.cache.redis_client import get_redis
from app.db.database import SessionLocal
from app.db.models import Order, Ticket, TicketComment, User
from evals.common import DATASET_DIR, EVAL_USER, FLOORS, load_jsonl

E2E_PORT = 8123
CHAT_TIMEOUT = 240.0


def _flush_semcache() -> None:
    """Deterministic cache state per run; degrade silently if Redis is down (the cache does
    too — the knowledge_cache flow will then fail loudly, which is the honest signal)."""
    try:
        r = get_redis()
        keys = list(r.scan_iter(match=b"semcache:*"))
        if keys:
            r.delete(*keys)
    except Exception:  # noqa: BLE001
        pass


def _spawn_api() -> tuple[subprocess.Popen, str]:
    base = f"http://localhost:{E2E_PORT}"
    # Refuse to adopt a stale server: if something already answers on the e2e port, our spawn
    # would fail to bind and the poll below would greenlight the SQUATTER — which we would
    # then fail to terminate in cleanup. Explicit E2E_API_URL is the supported way to reuse
    # a running API.
    try:
        httpx.get(f"{base}/healthz", timeout=1)
        raise RuntimeError(
            f"port {E2E_PORT} is already serving — stop it or set E2E_API_URL to use it"
        )
    except httpx.HTTPError:
        pass
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(E2E_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        try:
            if httpx.get(f"{base}/readyz", timeout=2).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("e2e API did not become ready on /readyz within 60s")


def _chat(client: httpx.Client, base: str, message: str, session_id: str) -> dict:
    resp = client.post(
        f"{base}/chat",
        json={"message": message, "user_id": EVAL_USER, "session_id": session_id},
        timeout=CHAT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _snapshot() -> tuple[set, set, set]:
    with SessionLocal() as s:
        return (
            set(s.scalars(select(Order.id))),
            set(s.scalars(select(Ticket.id))),
            set(s.scalars(select(TicketComment.id))),
        )


def _cleanup(baseline: tuple[set, set, set]) -> None:
    before_orders, before_tickets, before_comments = baseline
    with SessionLocal() as s:
        for c in s.scalars(select(TicketComment).where(TicketComment.id.notin_(before_comments))):
            s.delete(c)
        for t in s.scalars(select(Ticket).where(Ticket.id.notin_(before_tickets))):
            s.delete(t)
        for o in s.scalars(select(Order).where(Order.id.notin_(before_orders))):
            s.delete(o)
        s.commit()


def _demo_user_id(s) -> uuid.UUID:
    return s.scalar(select(User.id).where(User.email == EVAL_USER))


# --- flows -----------------------------------------------------------------------------------


def _flow_order(base: str, case: dict, decision: str) -> dict:
    """HITL order (> $500): chat run ends with draft->pending for the RIGHT user; then the
    approvals API decides from a fresh client. decision: "approve" -> placed, "reject" ->
    cancelled.

    The scripted 2-turn conversation sometimes isn't enough — the agent may ask one more
    clarifying question before acting (observed on the second nightly: no order row after 2
    turns). The contract is "the order reaches pending", not "in exactly N turns", so the
    flow answers like a real user would: up to two nudge turns, then it fails for real.
    """
    with SessionLocal() as s:
        before = set(s.scalars(select(Order.id)))
    sid = str(uuid.uuid4())
    nudges_used = 0
    with httpx.Client() as chat_client:
        for message in case["messages"]:
            _chat(chat_client, base, message, sid)
        for _ in range(2):
            with SessionLocal() as s:
                new_ids = set(s.scalars(select(Order.id))) - before
                # done only when the HITL contract is reached — a draft-only row means the
                # agent still hasn't requested approval, so keep answering
                if any(
                    (o.status, o.approval_state) == ("submitted", "pending")
                    for o in (s.get(Order, oid) for oid in new_ids)
                ):
                    break
            nudges_used += 1
            _chat(
                chat_client,
                base,
                "Yes, everything is confirmed — please go ahead and place the order now with "
                "my defaults. No further questions needed.",
                sid,
            )

    with SessionLocal() as s:
        demo_id = _demo_user_id(s)
        new = [s.get(Order, oid) for oid in set(s.scalars(select(Order.id))) - before]
        if not new:
            return {"ok": False, "detail": f"no order row created ({nudges_used} nudges used)"}
        # Identity assertion (user_tools DESIGN NOTE): every row this run created belongs to
        # the requesting user — none for anyone else.
        foreign = [o for o in new if o.user_id != demo_id]
        if foreign:
            return {"ok": False, "detail": f"{len(foreign)} order row(s) created for ANOTHER user"}
        pending = [o for o in new if (o.status, o.approval_state) == ("submitted", "pending")]
        if len(pending) != 1:
            states = [(o.status, o.approval_state) for o in new]
            return {"ok": False, "detail": f"expected exactly 1 pending order, got {states}"}
        order_id = pending[0].id

    # "Another process": a fresh client, after the agent run has ended (ADR-020).
    with httpx.Client() as approver:
        resp = approver.post(f"{base}/approvals/{order_id}/{decision}", timeout=30.0)
        if resp.status_code != 200:
            return {"ok": False, "detail": f"{decision} returned {resp.status_code}: {resp.text}"}

    expected = ("submitted", "approved") if decision == "approve" else ("cancelled", "rejected")
    with SessionLocal() as s:
        order = s.get(Order, order_id)
        got = (order.status, order.approval_state)
    nudge_note = f", {nudges_used} nudge(s)" if nudges_used else ""
    return {
        "ok": got == expected,
        "detail": (
            f"pending -> {decision} -> {got[0]}/{got[1]} "
            f"(expected {expected[0]}/{expected[1]}{nudge_note})"
        ),
    }


def _flow_incident_link(base: str, case: dict) -> dict:
    """Near-duplicate of a seeded open ticket: expect a comment on one of the target tickets
    (any open ticket about the same outage is a correct link) and NO new ticket."""
    with SessionLocal() as s:
        before_tickets = set(s.scalars(select(Ticket.id)))
        before_comments = set(s.scalars(select(TicketComment.id)))
    with httpx.Client() as client:
        _chat(client, base, case["report"], str(uuid.uuid4()))
    targets = set(case["link_targets"])
    with SessionLocal() as s:
        demo_id = _demo_user_id(s)
        new_tickets = set(s.scalars(select(Ticket.id))) - before_tickets
        new_comments = [
            c
            for c in s.scalars(
                select(TicketComment).where(TicketComment.id.notin_(before_comments))
            )
        ]
        linked = [
            c
            for c in new_comments
            if c.author_id == demo_id and s.get(Ticket, c.ticket_id).title in targets
        ]
        linked_title = s.get(Ticket, linked[0].ticket_id).title if linked else None
    ok = bool(linked) and not new_tickets
    return {
        "ok": ok,
        "detail": (
            f"linked to {linked_title!r}, no new ticket"
            if ok
            else f"new tickets={len(new_tickets)}, on-target comments={len(linked)}"
        ),
    }


def _flow_incident_create(base: str, case: dict) -> dict:
    """Fresh issue: expect exactly one NEW ticket, owned by the acting user, embedded at
    creation (invariant 3)."""
    with SessionLocal() as s:
        before_tickets = set(s.scalars(select(Ticket.id)))
    with httpx.Client() as client:
        _chat(client, base, case["report"], str(uuid.uuid4()))
    with SessionLocal() as s:
        demo_id = _demo_user_id(s)
        new_ids = set(s.scalars(select(Ticket.id))) - before_tickets
        if len(new_ids) != 1:
            return {"ok": False, "detail": f"expected exactly 1 new ticket, got {len(new_ids)}"}
        t = s.get(Ticket, next(iter(new_ids)))
        ok = t.user_id == demo_id and t.embedding is not None
        detail = (
            f"ticket {t.title!r} owner={'right user' if t.user_id == demo_id else 'WRONG USER'} "
            f"embedded={'yes' if t.embedding is not None else 'NO'}"
        )
    return {"ok": ok, "detail": detail}


def _flow_knowledge_cache(base: str, case: dict) -> dict:
    """Knowledge answer carries citations; a fresh-session paraphrase is served cached=true
    with the STORED citations (the M3 contract is part of the product now)."""
    with httpx.Client() as client:
        first = _chat(client, base, case["ask"], str(uuid.uuid4()))
        checks = {
            "first not cached": not first["cached"],
            "first has citations": bool(first["citations"]),
            "first has Sources": "Sources:" in first["answer"],
        }
        second = _chat(client, base, case["paraphrase"], str(uuid.uuid4()))
        checks["paraphrase cached"] = bool(second["cached"])
        checks["cached answer has citations"] = bool(second["citations"])
    failed = [name for name, ok in checks.items() if not ok]
    return {"ok": not failed, "detail": "all checks" if not failed else f"failed: {failed}"}


def _flow_refusal(base: str, case: dict) -> dict:
    """Out-of-KB question: refuses (no Sources), offers a ticket, zero citations, NOT cached —
    and NOT stored (re-asking in a fresh session must miss the cache again)."""
    with httpx.Client() as client:
        first = _chat(client, base, case["ask"], str(uuid.uuid4()))
        checks = {
            "not cached": not first["cached"],
            "zero citations": not first["citations"],
            "no Sources list": "Sources:" not in first["answer"],
            "offers a ticket": "ticket" in first["answer"].lower(),
        }
        second = _chat(client, base, case["ask"], str(uuid.uuid4()))
        checks["refusal was never stored"] = not second["cached"]
    failed = [name for name, ok in checks.items() if not ok]
    return {"ok": not failed, "detail": "all checks" if not failed else f"failed: {failed}"}


_FLOWS = {
    "order_approve": lambda base, case: _flow_order(base, case, "approve"),
    "order_reject": lambda base, case: _flow_order(base, case, "reject"),
    "incident_link": _flow_incident_link,
    "incident_create": _flow_incident_create,
    "knowledge_cache": _flow_knowledge_cache,
    "refusal": _flow_refusal,
}


def run_e2e(**_ignored) -> dict:
    """Run the e2e suite; retrieval-suite kwargs (k/threshold/refusal_mode) don't apply."""
    cases = load_jsonl(DATASET_DIR / "e2e.jsonl")
    baseline = _snapshot()
    _flush_semcache()

    external = os.environ.get("E2E_API_URL")
    proc = None
    if external:
        base = external.rstrip("/")
    else:
        proc, base = _spawn_api()

    rows = []
    try:
        for case in cases:
            try:
                result = _FLOWS[case["flow"]](base, case)
            except Exception as exc:  # noqa: BLE001 — a crashed flow is a failure, not a harness crash
                result = {"ok": False, "detail": f"<error: {exc.__class__.__name__}: {exc}>"}
            rows.append({"flow": case["flow"], **result})
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
        _cleanup(baseline)
        _flush_semcache()

    passed_flows = sum(1 for r in rows if r["ok"])
    report = {
        "suite": "e2e",
        "rows": rows,
        "aggregates": {"flows_passed": passed_flows, "n": len(rows)},
    }
    report["passed"] = (passed_flows / len(rows)) >= FLOORS["e2e"]["flow_pass_rate"]
    return report

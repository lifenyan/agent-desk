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

import json
import os
import subprocess
import sys
import time
import uuid

import httpx
from sqlalchemy import select, text

from app.cache.redis_client import get_redis
from app.db.database import SessionLocal
from app.db.models import CatalogItem, Order, Ticket, TicketComment, User, UserFact
from evals.common import DATASET_DIR, EVAL_USER, FLOORS, cost_latency_aggregates, load_jsonl

E2E_PORT = 8123
CHAT_TIMEOUT = 240.0
# Memory flows wait for the post-response background extraction (ADR-031) to land a
# user_facts row; one LLM call, so seconds — the deadline is generous, not a sleep.
FACT_POLL_TIMEOUT = 90.0


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


def _terminate(proc: subprocess.Popen) -> None:
    """Stop the suite-owned server WITHOUT ever raising: uvicorn's graceful shutdown waits for
    in-flight background work (the ADR-031 extraction task) and can outlast a polite timeout —
    observed live: a TimeoutExpired here skipped _cleanup entirely and left both a squatting
    server on the e2e port and trial rows in the DB. Escalate to SIGKILL instead."""
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _chat(
    client: httpx.Client, base: str, message: str, session_id: str, user: str = EVAL_USER
) -> dict:
    resp = client.post(
        f"{base}/chat",
        json={"message": message, "user_id": user, "session_id": session_id},
        timeout=CHAT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _snapshot() -> dict:
    """Ids of every row type e2e flows can create — plus the CONTENT of user_facts and of
    ticket status/priority (M5): the background extractor may UPDATE a seeded fact in place,
    and an incident-agent run may bump a seeded ticket's priority while linking (observed on
    the first baseline run: multi_intent left the seeded Wi-Fi ticket at high). Id-diffs
    can't see in-place updates, so both are restored field-by-field in cleanup."""
    with SessionLocal() as s:
        return {
            "orders": set(s.scalars(select(Order.id))),
            "tickets": set(s.scalars(select(Ticket.id))),
            "ticket_state": {
                t.id: (t.status, t.priority, t.category) for t in s.scalars(select(Ticket))
            },
            "comments": set(s.scalars(select(TicketComment.id))),
            "facts": {
                f.id: (f.fact_type, f.fact, f.source, f.confidence)
                for f in s.scalars(select(UserFact))
            },
            "sessions": set(s.scalars(text("SELECT session_id FROM agent_sessions")).all()),
        }


def _cleanup(baseline: dict) -> None:
    with SessionLocal() as s:
        for c in s.scalars(
            select(TicketComment).where(TicketComment.id.notin_(baseline["comments"]))
        ):
            s.delete(c)
        for t in s.scalars(select(Ticket).where(Ticket.id.notin_(baseline["tickets"]))):
            s.delete(t)
        # Seeded tickets a flow's agent mutated in place (priority bumps while linking).
        for t in s.scalars(select(Ticket).where(Ticket.id.in_(baseline["ticket_state"]))):
            before = baseline["ticket_state"][t.id]
            if (t.status, t.priority, t.category) != before:
                t.status, t.priority, t.category = before
        for o in s.scalars(select(Order).where(Order.id.notin_(baseline["orders"]))):
            s.delete(o)
        # user_facts: delete extracted rows, restore any seeded row the merge rule replaced.
        for f in s.scalars(select(UserFact)):
            before = baseline["facts"].get(f.id)
            if before is None:
                s.delete(f)
            elif (f.fact_type, f.fact, f.source, f.confidence) != before:
                f.fact_type, f.fact, f.source, f.confidence = before
        # SDK session rows (agent_messages cascade); keyed by uuid4, so never seeded.
        new_sessions = (
            set(s.scalars(text("SELECT session_id FROM agent_sessions")).all())
            - baseline["sessions"]
        )
        for sid in new_sessions:
            s.execute(text("DELETE FROM agent_sessions WHERE session_id = :sid"), {"sid": sid})
        s.commit()


def _user_id(s, email: str = EVAL_USER) -> uuid.UUID:
    return s.scalar(select(User.id).where(User.email == email))


# Kept name for the original flows' readability; new flows resolve their own acting user.
_demo_user_id = _user_id


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
    creation (invariant 3). `user` in the case overrides the acting user — the isolation
    variant runs as a non-demo user and asserts the row lands under THAT identity."""
    acting = case.get("user", EVAL_USER)
    with SessionLocal() as s:
        before_tickets = set(s.scalars(select(Ticket.id)))
    sid = str(uuid.uuid4())
    with httpx.Client() as client:
        _chat(client, base, case["report"], sid, acting)
        # Same contract-not-turn-count discipline as the order flows and refusal_to_ticket
        # (ADR-027 addendum): the agent may end its turn on a clarifying question instead of
        # creating — the 2026-07-11 nightly failed here with "got 0" on identical code that
        # passed the nights before and after (the known lost-report mode). One bounded nudge,
        # then it fails for real.
        with SessionLocal() as s:
            if set(s.scalars(select(Ticket.id))) == before_tickets:
                _chat(
                    client,
                    base,
                    "No further questions needed — please create the ticket now with the "
                    "details you have.",
                    sid,
                    acting,
                )
    with SessionLocal() as s:
        acting_id = _user_id(s, acting)
        new_ids = set(s.scalars(select(Ticket.id))) - before_tickets
        if len(new_ids) != 1:
            return {"ok": False, "detail": f"expected exactly 1 new ticket, got {len(new_ids)}"}
        t = s.get(Ticket, next(iter(new_ids)))
        ok = t.user_id == acting_id and t.embedding is not None
        detail = (
            f"ticket {t.title!r} owner={'right user' if t.user_id == acting_id else 'WRONG USER'}"
            f" ({acting}) embedded={'yes' if t.embedding is not None else 'NO'}"
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
    """Out-of-KB question: refuses (no Sources), offers a ticket, NOT cached — and NOT stored
    (re-asking in a fresh session must miss the cache again).

    Zero citations holds only for STAGE-1 refusals (evidence gate fails -> payloads skipped).
    A stage-2 refusal (gate passes, agent judges no coverage — the smartwatch probe, 0.611)
    correctly returns the ADJACENT articles as citations: ChatResponse.citations is documented
    as "retrieved sources put in front of the model", and the agent may name the adjacent
    article as possibly related. So the check is dataset-scoped via `stage1: true` — learned
    from the first 18-flow baseline run, disclosed in DECISIONS.md (M5 honest accounting)."""
    with httpx.Client() as client:
        first = _chat(client, base, case["ask"], str(uuid.uuid4()))
        checks = {
            "not cached": not first["cached"],
            "no Sources list": "Sources:" not in first["answer"],
            "offers a ticket": "ticket" in first["answer"].lower(),
        }
        if case.get("stage1"):
            checks["zero citations"] = not first["citations"]
        second = _chat(client, base, case["ask"], str(uuid.uuid4()))
        checks["refusal was never stored"] = not second["cached"]
    failed = [name for name, ok in checks.items() if not ok]
    return {"ok": not failed, "detail": "all checks" if not failed else f"failed: {failed}"}


def _flow_knowledge_basic(base: str, case: dict) -> dict:
    """Plain knowledge contract without the cache half: fresh answer with citations and the
    ADR-017 "Sources:" list (the cache pair stays its own flow — its paraphrase is MEASURED
    above the 0.75 threshold, and inventing new pairs by eye is how thresholds get vibed)."""
    with httpx.Client() as client:
        resp = _chat(client, base, case["ask"], str(uuid.uuid4()))
    checks = {
        "not cached": not resp["cached"],
        "has citations": bool(resp["citations"]),
        "has Sources": "Sources:" in resp["answer"],
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {"ok": not failed, "detail": "all checks" if not failed else f"failed: {failed}"}


def _flow_refusal_to_ticket(base: str, case: dict) -> dict:
    """The knowledge→incident edge (ADR-022) as product: an out-of-KB question refuses and
    offers a ticket; the user ACCEPTS in the same session; a ticket must exist afterwards —
    owned by the acting user, embedded — and only then."""
    with SessionLocal() as s:
        before_tickets = set(s.scalars(select(Ticket.id)))
    sid = str(uuid.uuid4())
    with httpx.Client() as client:
        first = _chat(client, base, case["ask"], sid)
        checks = {
            "refused (no Sources)": "Sources:" not in first["answer"],
            "offered a ticket": "ticket" in first["answer"].lower(),
        }
        with SessionLocal() as s:
            checks["no ticket before acceptance"] = (
                set(s.scalars(select(Ticket.id))) == before_tickets
            )
        _chat(client, base, case["accept"], sid)
        # Same contract-not-turn-count discipline as the order flows (ADR-027 addendum): the
        # incident agent may ask one more question (asset? priority?) before creating —
        # observed on the first trial run. One bounded nudge, then it fails for real.
        with SessionLocal() as s:
            if set(s.scalars(select(Ticket.id))) == before_tickets:
                _chat(
                    client,
                    base,
                    "No further questions needed — please create the ticket now with the "
                    "details you have.",
                    sid,
                )
    with SessionLocal() as s:
        demo_id = _user_id(s)
        new = [s.get(Ticket, t) for t in set(s.scalars(select(Ticket.id))) - before_tickets]
        checks["exactly 1 new ticket"] = len(new) == 1
        checks["right owner + embedded"] = bool(new) and all(
            t.user_id == demo_id and t.embedding is not None for t in new
        )
    failed = [name for name, ok in checks.items() if not ok]
    return {"ok": not failed, "detail": "all checks" if not failed else f"failed: {failed}"}


def _flow_multi_intent(base: str, case: dict) -> dict:
    """One message, two domains (incident + knowledge). The incident half is a hard side
    effect: the agent acted on the report (new ticket OR a comment on the user's existing
    one — demo already owns an open Wi-Fi ticket, so linking is correct too). The knowledge
    half may take a nudge turn (specialists return to the router on topic change; the router
    routes the FIRST actionable request first)."""
    with SessionLocal() as s:
        before_tickets = set(s.scalars(select(Ticket.id)))
        before_comments = set(s.scalars(select(TicketComment.id)))
    sid = str(uuid.uuid4())
    with httpx.Client() as client:
        resp = _chat(client, base, case["message"], sid)
        answer = resp["answer"]
        if case["knowledge_keyword"] not in answer.lower() and "Sources:" not in answer:
            resp = _chat(client, base, case["nudge"], sid)
            answer = resp["answer"]
    with SessionLocal() as s:
        demo_id = _user_id(s)
        new_tickets = [s.get(Ticket, t) for t in set(s.scalars(select(Ticket.id))) - before_tickets]
        new_comments = list(
            s.scalars(select(TicketComment).where(TicketComment.id.notin_(before_comments)))
        )
        incident_acted = any(t.user_id == demo_id for t in new_tickets) or any(
            c.author_id == demo_id for c in new_comments
        )
    checks = {
        "incident half acted (ticket or comment)": incident_acted,
        "knowledge half answered": case["knowledge_keyword"] in answer.lower()
        or "Sources:" in answer,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {"ok": not failed, "detail": "all checks" if not failed else f"failed: {failed}"}


def _flow_ticket_update(base: str, case: dict) -> dict:
    """update_ticket path: the user asks to bump their OWN existing ticket; assert the seeded
    row actually changed (priority), then restore it — this flow must leave the demo DB
    exactly as found (updates aren't caught by the id-diff cleanup)."""
    with SessionLocal() as s:
        demo_id = _user_id(s)
        ticket = s.scalar(
            select(Ticket).where(Ticket.user_id == demo_id, Ticket.title == case["ticket_title"])
        )
        if ticket is None:
            return {"ok": False, "detail": f"seeded ticket {case['ticket_title']!r} not found"}
        ticket_id, before_priority, before_status = ticket.id, ticket.priority, ticket.status
    try:
        sid = str(uuid.uuid4())
        with httpx.Client() as client:
            _chat(client, base, case["message"], sid)
            # ADR-027-addendum discipline: the agent may ask which ticket / confirm before
            # writing (observed on the first 18-flow run). One bounded confirmation turn;
            # the contract stays "the row changed", never "in exactly one turn".
            with SessionLocal() as s:
                if s.get(Ticket, ticket_id).priority != case["expected_priority"]:
                    _chat(
                        client,
                        base,
                        "Yes, that's the one — please set its priority to high now, "
                        "no further questions needed.",
                        sid,
                    )
        with SessionLocal() as s:
            t = s.get(Ticket, ticket_id)
            got_priority, got_status = t.priority, t.status
        ok = got_priority == case["expected_priority"] and got_status == before_status
        return {
            "ok": ok,
            "detail": (
                f"priority {before_priority} -> {got_priority} "
                f"(expected {case['expected_priority']}), status untouched: "
                f"{got_status == before_status}"
            ),
        }
    finally:
        with SessionLocal() as s:
            t = s.get(Ticket, ticket_id)
            t.priority, t.status = before_priority, before_status
            s.commit()


def _flow_order_autoplace(base: str, case: dict) -> dict:
    """≤ $500 order: places in ONE run with no approval round-trip (submitted/not_required —
    the other half of the ADR-020 gate), and its form_values honor the item's form_schema:
    only declared field names, every required field filled."""
    with SessionLocal() as s:
        before = set(s.scalars(select(Order.id)))
    sid = str(uuid.uuid4())
    with httpx.Client() as client:
        for message in case["messages"]:
            _chat(client, base, message, sid)
        for _ in range(2):
            with SessionLocal() as s:
                if set(s.scalars(select(Order.id))) - before:
                    break
            _chat(
                client,
                base,
                "Yes, everything is confirmed — please place the order now with my defaults.",
                sid,
            )
    with SessionLocal() as s:
        demo_id = _user_id(s)
        new = [s.get(Order, oid) for oid in set(s.scalars(select(Order.id))) - before]
        if len(new) != 1:
            return {"ok": False, "detail": f"expected exactly 1 new order, got {len(new)}"}
        order = new[0]
        schema_fields = {f["name"]: f for f in s.get(CatalogItem, order.item_id).form_schema}
        undeclared = set(order.form_values or {}) - set(schema_fields)
        missing_required = [
            name
            for name, f in schema_fields.items()
            if f.get("required") and not (order.form_values or {}).get(name)
        ]
        checks = {
            "right owner": order.user_id == demo_id,
            "placed without HITL": (order.status, order.approval_state)
            == ("submitted", "not_required"),
            "no undeclared form fields": not undeclared,
            "required fields filled": not missing_required,
        }
    failed = [name for name, ok in checks.items() if not ok]
    detail = "all checks" if not failed else f"failed: {failed}"
    if undeclared or missing_required:
        detail += f" (undeclared={sorted(undeclared)}, missing={missing_required})"
    return {"ok": not failed, "detail": detail}


def _flow_order_unorderable(base: str, case: dict) -> dict:
    """OS-incompatible item (windows-only AutoCAD vs the demo user's mac — measured failing
    CORRECTLY in M4): the run must end with NO order row, in any state."""
    with SessionLocal() as s:
        before = set(s.scalars(select(Order.id)))
    with httpx.Client() as client:
        _chat(client, base, case["message"], str(uuid.uuid4()))
    with SessionLocal() as s:
        new = [s.get(Order, oid) for oid in set(s.scalars(select(Order.id))) - before]
    if new:
        states = [(o.status, o.approval_state) for o in new]
        return {"ok": False, "detail": f"order row(s) created for incompatible item: {states}"}
    return {"ok": True, "detail": "no order row created (incompatibility surfaced instead)"}


def _flow_memory_carryover(base: str, case: dict) -> dict:
    """The ADR-031 loop as product: a durable fact mentioned in session A is extracted
    (user_facts row — the hard side effect), then a FRESH session B answers from the injected
    fact. Runs as a fact-less non-demo user so the demo user's seeded facts stay untouched;
    the run's rows are removed by the user_facts snapshot cleanup."""
    acting = case["user"]
    with SessionLocal() as s:
        acting_id = _user_id(s, acting)
        before_facts = set(s.scalars(select(UserFact.id).where(UserFact.user_id == acting_id)))
    with httpx.Client() as client:
        _chat(client, base, case["mention"], str(uuid.uuid4()), acting)
        deadline = time.time() + FACT_POLL_TIMEOUT
        new_facts = []
        while time.time() < deadline and not new_facts:
            with SessionLocal() as s:
                new_facts = list(
                    s.scalars(
                        select(UserFact).where(
                            UserFact.user_id == acting_id, UserFact.id.notin_(before_facts)
                        )
                    )
                )
            if not new_facts:
                time.sleep(2)
        if not new_facts:
            return {"ok": False, "detail": "no fact extracted within the poll window"}
        fact_summary = f"({new_facts[0].fact_type}) {new_facts[0].fact!r}"
        recall = _chat(client, base, case["recall"], str(uuid.uuid4()), acting)
    used = case["recall_keyword"] in recall["answer"].lower()
    return {
        "ok": used,
        "detail": (
            f"extracted {fact_summary}; session B "
            f"{'used it' if used else 'did NOT use it: ' + recall['answer'][:80]!r}"
        ),
    }


def _flow_chat_restart(base: str, case: dict, respawn=None) -> dict:
    """Chat history survives an API restart (the M2 e2e proved it for ORDER state; this is
    the chat-history half ADR-030 exists for): mention a distinctive token, kill + respawn
    the server, continue the SAME session and expect the token recalled. With an external
    server (E2E_API_URL) there is nothing we may restart — continuity is still asserted, the
    restart itself is skipped and disclosed in the detail."""
    sid = str(uuid.uuid4())
    with httpx.Client() as client:
        _chat(client, base, case["mention"], sid)
    restarted = False
    if respawn is not None:
        respawn()
        restarted = True
    with httpx.Client() as client:
        recall = _chat(client, base, case["recall"], sid)
    ok = case["token"].lower() in recall["answer"].lower()
    note = "across restart" if restarted else "NO restart (external server)"
    return {
        "ok": ok,
        "detail": (
            f"token {case['token']!r} {'recalled' if ok else 'LOST'} {note}"
            + ("" if ok else f": {recall['answer'][:80]!r}")
        ),
    }


def _flow_stream_parity(base: str, case: dict) -> dict:
    """M11 (ADR-048): /chat/stream is the same pipeline as the frozen /chat, proven live.
    A cache-miss knowledge query runs via SSE — deltas must arrive before `final`, and `final`
    must carry the full ChatResponse equivalent (citations, Sources contract, cached=false).
    Then the SAME query via POST /chat in a fresh session must come back cached=true with the
    IDENTICAL answer + citations: the stream's write-time cache store is readable by the
    frozen endpoint (write-gate parity), and the two contracts carry the same payload."""
    events: list[tuple[str, dict]] = []
    with (
        httpx.Client() as client,
        client.stream(
            "POST",
            f"{base}/chat/stream",
            json={"message": case["ask"], "user_id": EVAL_USER, "session_id": str(uuid.uuid4())},
            timeout=CHAT_TIMEOUT,
        ) as resp,
    ):
        resp.raise_for_status()
        event = data = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
            elif line == "" and event is not None:
                events.append((event, json.loads(data)))
                event = data = None

    kinds = [k for k, _ in events]
    finals = [payload for kind, payload in events if kind == "final"]
    deltas = [payload["text"] for kind, payload in events if kind == "delta"]
    final = finals[0] if finals else {}
    checks = {
        "no error frames": "error" not in kinds,
        "exactly one final, last": len(finals) == 1 and kinds[-1] == "final",
        "deltas arrived before final": bool(deltas) and kinds.index("delta") < len(kinds) - 1,
        "final not cached (cache-miss run)": not final.get("cached"),
        "final has citations": bool(final.get("citations")),
        "final has Sources": "Sources:" in final.get("answer", ""),
        "deltas concatenate to the final answer": "".join(deltas) == final.get("answer"),
    }
    with httpx.Client() as client:
        second = _chat(client, base, case["ask"], str(uuid.uuid4()))
    checks["/chat hit the stream-stored cache entry"] = bool(second["cached"])
    checks["/chat answer identical to streamed final"] = second["answer"] == final.get("answer")
    checks["/chat citations identical to streamed final"] = second["citations"] == final.get(
        "citations"
    )
    failed = [name for name, ok in checks.items() if not ok]
    detail = "all checks" if not failed else f"failed: {failed}"
    if deltas:
        detail += f" ({len(deltas)} deltas)"
    return {"ok": not failed, "detail": detail}


_FLOWS = {
    "order_approve": lambda base, case: _flow_order(base, case, "approve"),
    "order_reject": lambda base, case: _flow_order(base, case, "reject"),
    "incident_link": _flow_incident_link,
    "incident_create": _flow_incident_create,
    "knowledge_cache": _flow_knowledge_cache,
    "refusal": _flow_refusal,
    "knowledge_basic": _flow_knowledge_basic,
    "refusal_to_ticket": _flow_refusal_to_ticket,
    "multi_intent": _flow_multi_intent,
    "ticket_update": _flow_ticket_update,
    "order_autoplace": _flow_order_autoplace,
    "order_unorderable": _flow_order_unorderable,
    "memory_carryover": _flow_memory_carryover,
    "chat_restart": _flow_chat_restart,  # run_e2e passes respawn when it owns the server
    "stream_parity": _flow_stream_parity,
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

    def _respawn() -> None:
        """Kill + relaunch the suite-owned server (chat_restart flow). nonlocal so cleanup
        always terminates the CURRENT process, not a dead ancestor."""
        nonlocal proc
        _terminate(proc)
        proc, _ = _spawn_api()

    rows = []
    try:
        for case in cases:
            t0 = time.perf_counter()
            try:
                if case["flow"] == "chat_restart":
                    result = _flow_chat_restart(base, case, respawn=None if external else _respawn)
                else:
                    result = _FLOWS[case["flow"]](base, case)
            except Exception as exc:  # noqa: BLE001 — a crashed flow is a failure, not a harness crash
                result = {"ok": False, "detail": f"<error: {exc.__class__.__name__}: {exc}>"}
            # Wall-clock only: flows bill through the server process, whose SDK usage isn't
            # visible over HTTP — the M6 Langfuse wiring is where per-flow cost lands.
            rows.append(
                {
                    "flow": case["flow"],
                    **result,
                    "latency_s": round(time.perf_counter() - t0, 2),
                    "cost_usd": None,
                }
            )
    finally:
        if proc is not None:
            _terminate(proc)  # never raises — _cleanup below must always run
        _cleanup(baseline)
        _flush_semcache()

    passed_flows = sum(1 for r in rows if r["ok"])
    report = {
        "suite": "e2e",
        "rows": rows,
        "aggregates": {"flows_passed": passed_flows, "n": len(rows)},
    }
    report["cost_latency"] = cost_latency_aggregates(rows)
    report["passed"] = (passed_flows / len(rows)) >= FLOORS["e2e"]["flow_pass_rate"]
    return report

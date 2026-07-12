"""List/approve/reject pending catalog orders — human-in-the-loop for orders > $500.

The approval REQUEST was persisted by the agent run as orders.approval_state='pending' and
that run ended (ADR-005/ADR-020) — so these endpoints work from any fresh process with no
run state to resume: approving completes the order's placement through the same plain
catalog_tools DB path the agent tools use (ADR-004), rejecting cancels it. Approval authority
stays human-only: approve/reject are deliberately not agent tools.

Auth is deliberately out of scope — a stated cut line of the finished project (README
"Status: complete"): anyone who can reach the API is a "manager" (the approval_view UI is
the demo surface). The real safety boundary is that approval authority is human-only and
tool-less by design (ADR-005/020).
"""
# Implemented in M2.

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tools.catalog_tools import approve_order, list_pending_orders, reject_order

router = APIRouter(prefix="/approvals", tags=["approvals"])


class PendingOrder(BaseModel):
    order_id: str
    number: str  # user-facing ORDnnn (ADR-046) — what the approvals UI displays
    item: str
    price_usd: float
    requester: str
    requester_name: str
    org: str
    status: str
    approval_state: str
    form_values: dict


@router.get("", response_model=list[PendingOrder])
def pending() -> list[PendingOrder]:
    return [PendingOrder(**o) for o in list_pending_orders()]


def _decide(order_id: uuid.UUID, decide) -> PendingOrder:
    result = decide(str(order_id))
    if "error" in result:
        # not found / not pending — either way there is nothing to decide on
        raise HTTPException(status_code=409, detail=result["error"])
    return PendingOrder(**result)


@router.post("/{order_id}/approve", response_model=PendingOrder)
def approve(order_id: uuid.UUID) -> PendingOrder:
    """Approve: the order leaves 'pending' and is placed (status stays 'submitted')."""
    return _decide(order_id, approve_order)


@router.post("/{order_id}/reject", response_model=PendingOrder)
def reject(order_id: uuid.UUID) -> PendingOrder:
    """Reject: approval_state='rejected' and the order is cancelled (the M0 CHECK forbids
    a lingering 'pending' on a non-submitted order)."""
    return _decide(order_id, reject_order)

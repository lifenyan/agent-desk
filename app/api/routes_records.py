"""GET /tickets/{ref} + GET /orders/{ref} — detail pages behind the chat UI's record links.

The UI linkifies TKTnnn/ORDnnn mentions in agent replies (ADR-046); those links open
?ticket=/?order= pages that call these endpoints. Reads go through the tool modules'
plain detail functions (ADR-004 — routes never touch the tables directly). Auth is
deliberately out of scope, the same stated cut line as routes_approvals.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tools.catalog_tools import get_order_details
from app.tools.ticket_tools import get_ticket_details

router = APIRouter(tags=["records"])


class TicketComment(BaseModel):
    body: str
    created_at: str


class TicketDetail(BaseModel):
    number: str
    title: str
    description: str
    type: str
    category: str
    priority: str
    status: str
    comments: list[TicketComment]


class OrderDetail(BaseModel):
    number: str
    item: str
    price_usd: float
    status: str
    approval_state: str
    summary: str
    form_values: dict
    requester_name: str
    org: str


@router.get("/tickets/{ref}", response_model=TicketDetail)
async def ticket_detail(ref: str) -> TicketDetail:
    detail = await asyncio.to_thread(get_ticket_details, ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return TicketDetail(**detail)


@router.get("/orders/{ref}", response_model=OrderDetail)
async def order_detail(ref: str) -> OrderDetail:
    detail = await asyncio.to_thread(get_order_details, ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="order not found")
    return OrderDetail(**detail)

"""Manager approval view: card list of pending orders > $500 with approve/reject actions (HITL).

The human half of ADR-005/ADR-020: agent runs park expensive orders in
orders.approval_state='pending' and END; this view (run it with `make approvals`, port 8502)
is where a manager later approves (order placed) or rejects (order cancelled) — deliberately
a separate surface from the chat UI, because the approver is not the requester.
"""
# Implemented in M2. Visual pass shares theme.py with the chat UI; order ids stay in button
# keys/API calls only (never displayed — the approver identifies orders by item + requester).

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import theme  # noqa: E402

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="agentdesk — approvals", page_icon="✅")
st.markdown(theme.inject(), unsafe_allow_html=True)
st.markdown(theme.header("Approver desk", "manager approvals"), unsafe_allow_html=True)
st.caption("Orders above the $500 threshold wait here until a manager decides (human-in-the-loop).")


def _decide(order_id: str, action: str) -> None:
    try:
        response = httpx.post(f"{API_URL}/approvals/{order_id}/{action}", timeout=30.0)
        response.raise_for_status()
        order = response.json()
        icon = "✅" if action == "approve" else "🚫"
        st.toast(
            f"{icon} {action}d: {order.get('number', '')} — {order['item']} "
            f"for {order['requester_name']}"
        )
    except httpx.HTTPError as exc:
        st.error(f"{action} failed: `{exc}`")


try:
    pending = httpx.get(f"{API_URL}/approvals", timeout=30.0).json()
except httpx.HTTPError as exc:
    st.error(f"⚠️ Cannot reach the API at {API_URL}: `{exc}`")
    st.stop()

if not pending:
    st.success("Nothing waiting for approval. 🎉")

for order in pending:
    with st.container(border=True):
        left, right = st.columns([3, 1])
        with left:
            st.subheader(order["item"])
            st.markdown(
                f"**${order['price_usd']:,.2f}** — requested by **{order['requester_name']}** "
                f"({order['org']})"
            )
            st.markdown(
                theme.pills(
                    theme.pill(order.get("number", ""), "knowledge"),
                    theme.pill("awaiting approval", "pending"),
                ),
                unsafe_allow_html=True,
            )
            if order["form_values"]:
                with st.expander("Order form"):
                    for k, v in order["form_values"].items():
                        st.markdown(f"- **{k}**: {v}")
        with right:
            st.button(
                "Approve",
                key=f"approve-{order['order_id']}",
                type="primary",
                use_container_width=True,
                on_click=_decide,
                args=(order["order_id"], "approve"),
            )
            st.button(
                "Reject",
                key=f"reject-{order['order_id']}",
                use_container_width=True,
                on_click=_decide,
                args=(order["order_id"], "reject"),
            )

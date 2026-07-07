"""Streamlit chat UI: talks to the FastAPI /chat endpoint, renders citations and agent handoff traces."""
# Implemented in M1; M2 added per-browser-session continuity (session_id, ADR-019) and the
# acting-user picker (action agents need a trusted identity). M5 upgrades sessions to Postgres.

from __future__ import annotations

import os
import uuid

import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="agentdesk", page_icon="🎫")
st.title("🎫 agentdesk")
st.caption("AI service desk — ask about IT how-tos, order gear, or report an issue.")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    # One conversation per browser session (ADR-019); the backend keys its session store on this.
    st.session_state.session_id = str(uuid.uuid4())

with st.sidebar:
    # Trusted identity for the action agents (orders/tickets act as THIS user). In a real
    # deployment this comes from auth, never from user input — the picker stands in for login.
    user_id = st.text_input("Acting user (email)", value="demo.user@corp.com")
    st.caption(
        "Orders and tickets are created for this user. Manager approvals live in the "
        "approvals view: `make approvals` (port 8502)."
    )
    if st.button("New conversation"):
        st.session_state.messages = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()


def render_assistant(msg: dict) -> None:
    st.markdown(msg["answer"])
    if msg.get("agent"):
        st.caption(f"answered by: `{msg['agent']}` agent")
    if msg.get("citations"):
        with st.expander(f"📚 Sources ({len(msg['citations'])})"):
            for c in msg["citations"]:
                st.markdown(f"- **{c['title']}**  \n  `{c['article_id']}`")


for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_assistant(msg)
        else:
            st.markdown(msg["content"])

if prompt := st.chat_input("How do I reset my password?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Routing to a specialist…"):
            try:
                response = httpx.post(
                    f"{API_URL}/chat",
                    json={
                        "message": prompt,
                        "user_id": user_id or None,
                        "session_id": st.session_state.session_id,
                    },
                    timeout=120.0,
                )
                response.raise_for_status()
                data = response.json()
                msg = {
                    "role": "assistant",
                    "answer": data["answer"],
                    "agent": data.get("agent"),
                    "citations": data.get("citations", []),
                }
            except httpx.HTTPError as exc:
                msg = {
                    "role": "assistant",
                    "answer": f"⚠️ Backend error: `{exc}` — is the API up at {API_URL}?",
                    "agent": None,
                    "citations": [],
                }
        render_assistant(msg)
    st.session_state.messages.append(msg)

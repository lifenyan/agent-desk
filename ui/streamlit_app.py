"""Streamlit chat UI: talks to the FastAPI /chat endpoint, renders citations and agent handoff traces."""
# Implemented in M1; M2 added per-browser-session continuity (session_id, ADR-019) and the
# acting-user picker (action agents need a trusted identity). M5 upgrades sessions to Postgres.
# The visual pass: theme.py + .streamlit/config.toml; citations became links to the /articles
# page (ids live in URLs only — never displayed), and the answer's "Sources:" footer is
# stripped for DISPLAY only (the ADR-017 contract text is untouched in the API/cache).

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

import httpx
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import theme  # noqa: E402

API_URL = os.environ.get("API_URL", "http://localhost:8000")

# Per-agent avatars: the handoff target is visible at a glance in the transcript.
AVATARS = {"knowledge": "📚", "fulfillment": "🛒", "incident": "🛠️", "guardrail": "🛡️"}

# The knowledge agent's output contract (ADR-017) ends answers with a "Sources: …" footer that
# includes article ids. The citations panel below renders the same sources as clickable cards,
# so the footer is redundant on screen — strip it from the DISPLAYED text only.
_SOURCES_FOOTER = re.compile(r"\n+Sources:\s*\n.*\Z", re.DOTALL)


def display_answer(answer: str) -> str:
    return _SOURCES_FOOTER.sub("", answer).rstrip()


st.set_page_config(page_title="agentdesk", page_icon="🎫")
st.markdown(theme.inject(), unsafe_allow_html=True)


# --- Article page (?article=<id>): the citation-link target, stateless on purpose ------------
# Links open in a NEW tab, so this render path never touches the chat tab's session state.
if article_id := st.query_params.get("article"):
    try:
        response = httpx.get(f"{API_URL}/articles/{article_id}", timeout=15.0)
        response.raise_for_status()
        article = response.json()
    except httpx.HTTPError:
        st.markdown(theme.header("agentdesk", "knowledge base"), unsafe_allow_html=True)
        st.error("This article doesn't exist (or is no longer published).")
        st.stop()
    st.markdown(theme.header("agentdesk", "knowledge base"), unsafe_allow_html=True)
    st.title(article["title"])
    meta = [
        theme.pill(article["category"], "knowledge"),
        theme.pill(article["doc_type"]),
    ]
    if article.get("version"):
        meta.append(theme.pill(f"v{article['version']}"))
    meta.append(theme.pill(f"updated {article['updated_at'][:10]}"))
    st.markdown(f'<div class="ad-meta">{"".join(meta)}</div>', unsafe_allow_html=True)
    st.divider()
    st.markdown(article["body"])
    st.stop()


# --- Chat page -------------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    # One conversation per browser session (ADR-019); the backend keys its session store on this.
    st.session_state.session_id = str(uuid.uuid4())
if "api_ok" not in st.session_state:
    try:
        st.session_state.api_ok = httpx.get(f"{API_URL}/healthz", timeout=5.0).status_code == 200
    except httpx.HTTPError:
        st.session_state.api_ok = False

st.markdown(theme.header("agentdesk", "AI service desk"), unsafe_allow_html=True)
st.caption("Ask about IT how-tos, order gear, or report an issue.")

with st.sidebar:
    st.markdown(
        theme.status(
            st.session_state.api_ok, "API ONLINE" if st.session_state.api_ok else "API OFFLINE"
        ),
        unsafe_allow_html=True,
    )
    st.divider()
    # Trusted identity for the action agents (orders/tickets act as THIS user). In a real
    # deployment this comes from auth, never from user input — the picker stands in for login.
    user_id = st.text_input("Acting user (email)", value="demo.user@corp.com")
    st.caption(
        "Orders and tickets are created for this user. Manager approvals live in the "
        "separate approvals view."
    )
    if st.button("✦ New conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()
    turns = sum(1 for m in st.session_state.messages if m["role"] == "user")
    if turns:
        st.caption(f"{turns} turn{'s' if turns > 1 else ''} in this conversation")


def render_assistant(msg: dict) -> None:
    st.markdown(display_answer(msg["answer"]))
    badges = []
    if msg.get("cached"):
        badges.append(theme.pill("⚡ cached · semantic match", "cache"))
    elif msg.get("agent"):
        badges.append(theme.pill(f"{msg['agent']} agent", msg["agent"]))
    if badges:
        st.markdown(theme.pills(*badges), unsafe_allow_html=True)
    if msg.get("citations"):
        with st.expander(f"📚 Sources ({len(msg['citations'])})"):
            cards = "".join(
                theme.citation_link(c["title"], c["article_id"]) for c in msg["citations"]
            )
            st.markdown(f'<div class="ad-citations">{cards}</div>', unsafe_allow_html=True)


def avatar_for(msg: dict) -> str:
    if msg.get("cached"):
        return "⚡"
    return AVATARS.get(msg.get("agent") or "", "🤖")


for msg in st.session_state.messages:
    if msg["role"] == "assistant":
        with st.chat_message("assistant", avatar=avatar_for(msg)):
            render_assistant(msg)
    else:
        with st.chat_message("user", avatar="🧑‍💻"):
            st.markdown(msg["content"])

if prompt := st.chat_input("How do I reset my password?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(prompt)

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
                "cached": data.get("cached", False),
            }
        except httpx.HTTPError as exc:
            msg = {
                "role": "assistant",
                "answer": f"⚠️ Backend error: `{exc}` — is the API up at {API_URL}?",
                "agent": None,
                "citations": [],
            }
    with st.chat_message("assistant", avatar=avatar_for(msg)):
        render_assistant(msg)
    st.session_state.messages.append(msg)

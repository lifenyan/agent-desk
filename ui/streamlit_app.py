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

# Record numbers (ADR-046) in agent replies become links to the ?ticket=/?order= detail
# pages — display-side, deterministic, no agent-instruction involvement. Markdown links
# open in a new tab, so the running chat is never reloaded (same as citation links).
_RECORD_LINKS = [
    (re.compile(r"\b(TKT\d{3,})\b"), "ticket"),
    (re.compile(r"\b(ORD\d{3,})\b"), "order"),
]


def display_answer(answer: str) -> str:
    text = _SOURCES_FOOTER.sub("", answer).rstrip()
    for rx, kind in _RECORD_LINKS:
        text = rx.sub(rf"[\1](/?{kind}=\1)", text)
    return text


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
        st.markdown(theme.header("Agent desk", "knowledge base"), unsafe_allow_html=True)
        st.error("This article doesn't exist (or is no longer published).")
        st.stop()
    st.markdown(theme.header("Agent desk", "knowledge base"), unsafe_allow_html=True)
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


# --- Ticket page (?ticket=TKTnnn): the record-link target, stateless like the article page --
if ticket_ref := st.query_params.get("ticket"):
    st.markdown(theme.header("Agent desk", "support ticket"), unsafe_allow_html=True)
    try:
        response = httpx.get(f"{API_URL}/tickets/{ticket_ref}", timeout=15.0)
        response.raise_for_status()
        ticket = response.json()
    except httpx.HTTPError:
        st.error("This ticket doesn't exist.")
        st.stop()
    st.title(f"{ticket['number']} — {ticket['title']}")
    pills = "".join(
        [
            theme.pill(ticket["status"], "incident"),
            theme.pill(f"priority: {ticket['priority']}"),
            theme.pill(ticket["category"]),
            theme.pill(ticket["type"]),
        ]
    )
    st.markdown(f'<div class="ad-meta">{pills}</div>', unsafe_allow_html=True)
    st.divider()
    st.markdown(ticket["description"])
    if ticket["comments"]:
        st.subheader(f"Activity ({len(ticket['comments'])})")
        for c in ticket["comments"]:
            with st.container(border=True):
                st.markdown(c["body"])
                st.caption(c["created_at"][:16].replace("T", " "))
    st.stop()


# --- Order page (?order=ORDnnn) ------------------------------------------------------------
if order_ref := st.query_params.get("order"):
    st.markdown(theme.header("Agent desk", "catalog order"), unsafe_allow_html=True)
    try:
        response = httpx.get(f"{API_URL}/orders/{order_ref}", timeout=15.0)
        response.raise_for_status()
        order = response.json()
    except httpx.HTTPError:
        st.error("This order doesn't exist.")
        st.stop()
    st.title(f"{order['number']} — {order['item']}")
    st.markdown(
        f'<div class="ad-meta">{theme.pill(order["summary"], "fulfillment")}</div>',
        unsafe_allow_html=True,
    )
    st.divider()
    st.markdown(
        f"**${order['price_usd']:,.2f}** — requested by **{order['requester_name']}** "
        f"({order['org']})"
    )
    if order["form_values"]:
        st.subheader("Order form")
        for k, v in order["form_values"].items():
            st.markdown(f"- **{k}**: {v}")
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

st.markdown(theme.header("Agent desk", "AI service desk"), unsafe_allow_html=True)
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


# The button submits this on the user's behalf: it accepts the knowledge agent's standing
# refusal-offer (ADR-017), so the knowledge→incident edge files a ticket with full context.
HUMAN_HANDOFF_MESSAGE = (
    "Yes, please open a support ticket for this right away with the information you already "
    "have — don't wait for more details from me — and have someone from IT follow up."
)

# Bounded auto-retry (the Slack runner's re-submit-once pattern): the backend appends the
# TKTnnn of any real write to the answer, so a reply WITHOUT a number provably means no
# ticket exists — observed live as "Request submitted…" with zero tool writes. One nudge.
TICKET_NUDGE_MESSAGE = (
    "No ticket number came back, which means the ticket was NOT actually created. Call "
    "create_ticket right now with the information you already have and give me its TKT number."
)
_TKT_IN_ANSWER = re.compile(r"\bTKT\d{3,}\b")


def _is_refusal(msg: dict) -> bool:
    """ADR-017 contract: knowledge answers always carry citations; refusals never do."""
    return msg.get("agent") == "knowledge" and not msg.get("citations") and not msg.get("cached")


def render_assistant(msg: dict, *, key: int | None = None) -> None:
    st.markdown(display_answer(msg["answer"]))
    badges = []
    if msg.get("cached"):
        badges.append(theme.pill("⚡ cached · semantic match", "cache"))
    elif msg.get("agent"):
        badges.append(theme.pill(f"{msg['agent']} agent", msg["agent"]))
    if badges:
        st.markdown(theme.pills(*badges), unsafe_allow_html=True)
    if msg.get("citations"):
        with st.expander(f"📚 Sources ({len(msg['citations'])})", expanded=True):
            cards = "".join(
                theme.citation_link(c["title"], c["article_id"]) for c in msg["citations"]
            )
            st.markdown(f'<div class="ad-citations">{cards}</div>', unsafe_allow_html=True)
    # Only the newest refusal gets the escalation buttons (key is its position in history —
    # stable across reruns, so the click is never lost to a changing widget key).
    if key is not None and _is_refusal(msg):
        left, right = st.columns(2)
        with left:
            if st.button("🎫 Create a ticket", key=f"ticket-{key}", use_container_width=True):
                st.session_state.pending_prompt = HUMAN_HANDOFF_MESSAGE
                st.session_state.expect_ticket = True
                st.session_state.ticket_nudged = False
        with right:
            # Deliberately inert (product mock — no live-agent backend exists). The click
            # still reruns the script, which redraws the same transcript: a visible no-op.
            st.button("💬 Connect to live agent", key=f"live-{key}", use_container_width=True)


def avatar_for(msg: dict) -> str:
    if msg.get("cached"):
        return "⚡"
    return AVATARS.get(msg.get("agent") or "", "🤖")


last_idx = len(st.session_state.messages) - 1
for i, msg in enumerate(st.session_state.messages):
    if msg["role"] == "assistant":
        with st.chat_message("assistant", avatar=avatar_for(msg)):
            render_assistant(msg, key=i if i == last_idx else None)
    else:
        with st.chat_message("user", avatar="🧑‍💻"):
            st.markdown(msg["content"])

prompt = st.chat_input("How do I reset my password?")
if not prompt:
    prompt = st.session_state.pop("pending_prompt", None)
if prompt:
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
    # Append + rerun (rather than rendering in place): the transcript always draws through the
    # history loop, so the newest refusal's escalation button exists the moment it's answered.
    st.session_state.messages.append(msg)
    if (
        st.session_state.pop("expect_ticket", False)
        and msg.get("agent")  # a backend error is not the model failing to file
        and not _TKT_IN_ANSWER.search(msg["answer"])
        and not st.session_state.get("ticket_nudged")
    ):
        # Escalation turn came back with no ticket number => provably no ticket. Retry once.
        st.session_state.ticket_nudged = True
        st.session_state.expect_ticket = True
        st.session_state.pending_prompt = TICKET_NUDGE_MESSAGE
    st.rerun()

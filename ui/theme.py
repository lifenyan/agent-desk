"""Shared look-and-feel for the two Streamlit surfaces (chat + approvals).

One place for the CSS and the small HTML helpers so the surfaces read as one product.
Everything here is display-only: no state, no API calls. Selectors target Streamlit's
data-testids defensively — a missed selector degrades to unstyled, never breaks.
"""

from __future__ import annotations

import html

# Palette: deep-space navy base, cyan→violet accent ramp. Mirrors .streamlit/config.toml.
ACCENT = "#22d3ee"
ACCENT_2 = "#8b5cf6"

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

:root {{
    --ad-accent: {ACCENT};
    --ad-accent-2: {ACCENT_2};
    --ad-bg: #0a0f1e;
    --ad-panel: rgba(148, 163, 184, 0.06);
    --ad-border: rgba(148, 163, 184, 0.18);
    --ad-muted: #94a3b8;
}}

html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}

/* Ambient glow behind the whole app */
.stApp {{
    background:
        radial-gradient(60rem 30rem at 15% -10%, rgba(34, 211, 238, 0.09), transparent 60%),
        radial-gradient(50rem 30rem at 95% 0%, rgba(139, 92, 246, 0.10), transparent 55%),
        var(--ad-bg);
}}

/* Wordmark header */
.ad-header {{ padding: 0.2rem 0 0.6rem 0; }}
.ad-wordmark {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 2.1rem; font-weight: 600; letter-spacing: -0.02em;
    background: linear-gradient(90deg, var(--ad-accent) 0%, var(--ad-accent-2) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
}}
.ad-tagline {{
    color: var(--ad-muted); font-size: 0.78rem; font-weight: 600;
    letter-spacing: 0.22em; text-transform: uppercase; margin-top: 0.1rem;
}}

/* Chat bubbles: glass cards with a hairline border */
[data-testid="stChatMessage"] {{
    background: var(--ad-panel);
    border: 1px solid var(--ad-border);
    border-radius: 14px;
    padding: 0.9rem 1.1rem;
    backdrop-filter: blur(6px);
}}

/* Sidebar: slightly deeper panel + hairline divider */
[data-testid="stSidebar"] {{
    background: rgba(10, 15, 30, 0.92);
    border-right: 1px solid var(--ad-border);
}}

/* Chat input: accent focus ring */
[data-testid="stChatInput"] textarea {{ font-family: 'Inter', sans-serif; }}
[data-testid="stChatInput"] > div {{
    border: 1px solid var(--ad-border) !important;
    border-radius: 12px !important;
}}
[data-testid="stChatInput"] > div:focus-within {{
    border-color: var(--ad-accent) !important;
    box-shadow: 0 0 0 1px var(--ad-accent), 0 0 18px rgba(34, 211, 238, 0.25) !important;
}}

/* Buttons: gradient primary, quiet secondary */
.stButton > button[kind="primary"] {{
    background: linear-gradient(90deg, var(--ad-accent) 0%, var(--ad-accent-2) 100%);
    color: #06101f; font-weight: 700; border: none;
}}
.stButton > button {{ border-radius: 10px; }}

/* Expanders as panels */
[data-testid="stExpander"] {{
    border: 1px solid var(--ad-border); border-radius: 12px;
    background: var(--ad-panel);
}}

/* Pills (agent badge, cache badge, order state) */
.ad-pills {{ margin-top: 0.55rem; display: flex; gap: 0.4rem; flex-wrap: wrap; }}
.ad-pill {{
    display: inline-block; padding: 0.14rem 0.6rem; border-radius: 999px;
    font-family: 'JetBrains Mono', monospace; font-size: 0.68rem; font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase;
    border: 1px solid var(--ad-border); color: var(--ad-muted);
}}
.ad-pill.knowledge   {{ color: #22d3ee; border-color: rgba(34, 211, 238, 0.45); background: rgba(34, 211, 238, 0.08); }}
.ad-pill.fulfillment {{ color: #a78bfa; border-color: rgba(167, 139, 250, 0.45); background: rgba(167, 139, 250, 0.08); }}
.ad-pill.incident    {{ color: #fbbf24; border-color: rgba(251, 191, 36, 0.45); background: rgba(251, 191, 36, 0.08); }}
.ad-pill.cache       {{ color: #34d399; border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.08); }}
.ad-pill.pending     {{ color: #fbbf24; border-color: rgba(251, 191, 36, 0.45); background: rgba(251, 191, 36, 0.08); }}
.ad-pill.guardrail   {{ color: #f87171; border-color: rgba(248, 113, 113, 0.45); background: rgba(248, 113, 113, 0.08); }}

/* Citation link cards */
.ad-citations {{ display: flex; flex-direction: column; gap: 0.45rem; }}
a.ad-citation, a.ad-citation:visited {{
    display: block; padding: 0.6rem 0.85rem; border-radius: 10px;
    border: 1px solid var(--ad-border); background: var(--ad-panel);
    color: inherit; text-decoration: none; transition: border-color .15s, box-shadow .15s;
}}
a.ad-citation:hover {{
    border-color: var(--ad-accent);
    box-shadow: 0 0 14px rgba(34, 211, 238, 0.18);
}}
.ad-citation-title {{ font-weight: 600; font-size: 0.92rem; }}
.ad-citation-hint {{
    color: var(--ad-muted); font-size: 0.72rem; font-family: 'JetBrains Mono', monospace;
    margin-top: 0.15rem;
}}

/* Live status dot */
.ad-status {{ display: flex; align-items: center; gap: 0.45rem;
    font-family: 'JetBrains Mono', monospace; font-size: 0.72rem;
    letter-spacing: 0.08em; color: var(--ad-muted); }}
.ad-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.ad-dot.ok {{ background: #34d399; box-shadow: 0 0 8px rgba(52, 211, 153, 0.9); animation: ad-pulse 2.2s infinite; }}
.ad-dot.down {{ background: #f87171; box-shadow: 0 0 8px rgba(248, 113, 113, 0.9); }}
@keyframes ad-pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.45; }} }}

/* Article page meta row */
.ad-meta {{ display: flex; gap: 0.4rem; flex-wrap: wrap; margin: 0.4rem 0 0.2rem 0; }}
</style>
"""


def inject() -> str:
    """The CSS block — render once per page with st.markdown(..., unsafe_allow_html=True)."""
    return _CSS


def header(title: str, tagline: str) -> str:
    return (
        '<div class="ad-header">'
        f'<div class="ad-wordmark">{html.escape(title)}</div>'
        f'<div class="ad-tagline">{html.escape(tagline)}</div>'
        "</div>"
    )


def pill(label: str, kind: str = "") -> str:
    return f'<span class="ad-pill {html.escape(kind)}">{html.escape(label)}</span>'


def pills(*items: str) -> str:
    return f'<div class="ad-pills">{"".join(items)}</div>'


def citation_link(title: str, article_id: str) -> str:
    """One citation as a link card. The id travels ONLY in the href (opens the article page
    in a new tab, so the running chat is never reloaded); the visible text is title-only."""
    return (
        f'<a class="ad-citation" href="?article={html.escape(article_id)}" target="_blank" '
        'rel="noopener">'
        f'<div class="ad-citation-title">{html.escape(title)}</div>'
        '<div class="ad-citation-hint">open article ↗</div>'
        "</a>"
    )


def status(ok: bool, label: str) -> str:
    dot = "ok" if ok else "down"
    return f'<div class="ad-status"><span class="ad-dot {dot}"></span>{html.escape(label)}</div>'

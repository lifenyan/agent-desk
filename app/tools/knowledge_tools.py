"""Knowledge tools: search_knowledge_articles (hybrid RAG), get_release_notes.

The ONLY place retrieval touches the database (ADR-004): agents call these; these call
rag.hybrid_search with a session they own. Plain functions first (unit-testable, reused by
evals), then wrapped with @function_tool for the Agents SDK (`*_tool` names).

Each payload carries `sufficient_evidence` — the ADR-017 refusal gate (top cosine similarity vs
threshold) computed HERE, deterministically, so the agent never eyeballs raw scores to decide
whether it may answer.

`category` is typed as the `TicketCategory` enum, not a free `str`: the SDK emits it into the
tool's JSON schema as an `enum`, so under strict structured outputs the model is constrained at
decode time to a valid value — the format is enforced at generation, not merely requested in the
docstring. The manual value check below is kept as belt-and-suspenders for DIRECT/programmatic
callers (evals, tests, future code), which bypass the schema.
"""
# Implemented in M1; category constrained to the TicketCategory enum in the M1 follow-up.

from __future__ import annotations

from agents import function_tool

from app.config import get_settings
from app.db.database import SessionLocal
from app.db.models import DocType, TicketCategory
from app.rag.hybrid_search import SearchResult, hybrid_search, top_cosine

_VALID_CATEGORIES = {c.value for c in TicketCategory}


def _payload(results: list[SearchResult]) -> dict:
    settings = get_settings()
    best = top_cosine(results)
    sufficient = best >= settings.retrieval_refusal_threshold
    return {
        "sufficient_evidence": sufficient,
        "top_cosine_similarity": round(best, 4),
        "guidance": (
            "Retrieval found similar articles. Before answering, verify they cover the user's "
            "SPECIFIC device/product/topic (an adjacent article is not coverage); if they do, "
            "answer from these chunks and cite article titles + ids."
            if sufficient
            else "No article covers this well enough. Do NOT improvise an answer; tell the "
            "user the knowledge base has no article on this and offer to open a ticket."
        ),
        "results": [
            {
                "article_id": str(r.article_id),
                "article_title": r.article_title,
                "chunk_index": r.chunk_index,
                "content": r.content,
                "category": r.category,
                "doc_type": r.doc_type,
                "version": r.version,
                "cosine_similarity": round(r.cosine_sim, 4) if r.cosine_sim is not None else None,
                "rrf_score": round(r.rrf_score, 6),
            }
            for r in results
        ],
    }


def search_knowledge_articles(
    query: str, category: TicketCategory | None = None, version: str | None = None
) -> dict:
    """Search the IT knowledge base with hybrid (semantic + keyword) retrieval.

    Args:
        query: The search query. Use the user's key terms; rephrase jargon into likely
            knowledge-base wording (e.g. "can't get online" -> "network connection troubleshooting").
        category: Optional filter — one of the ticket categories (accounts, software, hardware,
            network, email, other). The schema constrains this to valid values.
        version: Optional product version filter, e.g. "v5.1".
    """
    # Schema-constrained on the agent path; this guards direct/programmatic callers. Accepts a
    # TicketCategory or a bare str (TicketCategory is a StrEnum, so it flows into SQL unchanged).
    if category is not None and category not in _VALID_CATEGORIES:
        return {
            "error": f"invalid category {category!r}; valid: {sorted(_VALID_CATEGORIES)}",
            "sufficient_evidence": False,
            "results": [],
        }
    with SessionLocal() as session:
        results = hybrid_search(session, query, category=category, version=version)
    return _payload(results)


def get_release_notes(version: str, compare_with: str | None = None) -> dict:
    """Fetch release notes for a product version, optionally alongside a second version to compare.

    Args:
        version: Version whose release notes to fetch, e.g. "v5.2".
        compare_with: Optional second version for a comparison, e.g. "v5.1".
    """

    def _notes(v: str) -> dict:
        with SessionLocal() as session:
            results = hybrid_search(
                session,
                f"release notes what's new in {v}",
                version=v,
                doc_type=DocType.release_notes.value,
            )
        return _payload(results)

    payload: dict = {"version": version, "notes": _notes(version)}
    if compare_with:
        payload["compare_with"] = compare_with
        payload["comparison_notes"] = _notes(compare_with)
    return payload


# --- Agents SDK wrappers (schema derived from the signatures + docstrings above) ---
search_knowledge_articles_tool = function_tool(search_knowledge_articles)
get_release_notes_tool = function_tool(get_release_notes)

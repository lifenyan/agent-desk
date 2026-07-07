"""Long-term memory CRUD over the user_facts table (fact_type, fact, confidence)."""
# Implemented in M5 (ADR-031). These are plain user-scoped functions in the tools discipline
# (ADR-004: SessionLocal is the only DB path; identity is the API's trusted user reference,
# never an LLM argument) but deliberately NOT agent tools — the lifecycle is owned by
# routes_chat (inject at session start) and extraction.py (extract at run end), the same
# "plain function, not a tool" precedent as approve_order/reject_order (ADR-020).

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import User, UserFact

# Injection skips low-confidence beliefs (user_facts.confidence column contract): a wrong fact
# confidently injected steers every answer in the session. Seeded facts sit at 0.6–0.95.
INJECTION_MIN_CONFIDENCE = 0.5


@dataclass(frozen=True)
class FactCandidate:
    """One extracted fact, before the merge rule decides whether it lands in the table."""

    fact_type: str
    fact: str
    confidence: float


def _resolve_user(session: Session, user_ref: str) -> User | None:
    """Trusted user reference (UUID or login email) -> User; same resolution rule as
    user_tools.resolve_acting_user, minus the RunContextWrapper plumbing."""
    try:
        return session.get(User, uuid.UUID(user_ref))
    except ValueError:
        return session.scalar(select(User).where(User.email == user_ref))


def get_user_facts(user_ref: str) -> list[UserFact]:
    """All stored facts for the user, newest first. Empty for unknown users — memory must
    never turn a chat request into an error."""
    with SessionLocal() as session:
        user = _resolve_user(session, user_ref)
        if user is None:
            return []
        return list(
            session.scalars(
                select(UserFact)
                .where(UserFact.user_id == user.id)
                .order_by(UserFact.updated_at.desc())
            )
        )


def injection_message(user_ref: str) -> dict | None:
    """The session-start system item (ADR-031): the user's confident facts, formatted for the
    transcript. None when there is nothing worth injecting."""
    facts = [f for f in get_user_facts(user_ref) if f.confidence >= INJECTION_MIN_CONFIDENCE]
    if not facts:
        return None
    lines = "\n".join(f"- ({f.fact_type}) {f.fact}" for f in facts)
    return {
        "role": "system",
        "content": (
            "Known facts about this user, remembered from previous conversations. Use them "
            "instead of re-asking, but let anything the user says NOW override them:\n" + lines
        ),
    }


def apply_extracted_facts(
    user_ref: str, candidates: list[FactCandidate], source: str | None = None
) -> dict[str, int]:
    """Merge extracted candidates into user_facts; returns counts for logging/tests.

    Merge rule (the fact_type unique constraint is the dedup key — contradictions REPLACE,
    never accumulate): unknown fact_type inserts; same fact_type with the same normalized text
    is a duplicate and is skipped; different text replaces only when the new confidence is at
    least the old one (a hesitant extraction never overwrites a confident belief; ties go to
    newer, per the updated_at column contract).
    """
    inserted = updated = skipped = 0
    with SessionLocal() as session:
        user = _resolve_user(session, user_ref)
        if user is None:
            return {"inserted": 0, "updated": 0, "skipped": len(candidates)}
        existing = {
            f.fact_type: f
            for f in session.scalars(select(UserFact).where(UserFact.user_id == user.id))
        }
        for cand in candidates:
            confidence = min(max(cand.confidence, 0.0), 1.0)
            old = existing.get(cand.fact_type)
            if old is None:
                row = UserFact(
                    user_id=user.id,
                    fact_type=cand.fact_type,
                    fact=cand.fact,
                    source=source,
                    confidence=confidence,
                )
                session.add(row)
                existing[cand.fact_type] = row
                inserted += 1
            elif cand.fact.strip().casefold() == old.fact.strip().casefold():
                skipped += 1
            elif confidence >= old.confidence:
                old.fact = cand.fact
                old.confidence = confidence
                old.source = source
                updated += 1
            else:
                skipped += 1
        session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}

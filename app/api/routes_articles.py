"""GET /articles/{id} — read one knowledge article, for the chat UI's clickable citations.

A product surface like routes_approvals, not an agent path: the chat UI links each citation
to a rendered article page instead of printing raw ids (ids stay in URLs only). Read-only,
published articles only — drafts/archived never back citations, so serving them here would
leak unvetted content.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.database import SessionLocal
from app.db.models import ArticleStatus, KnowledgeArticle

router = APIRouter(tags=["articles"])


class ArticleResponse(BaseModel):
    article_id: str
    title: str
    category: str
    doc_type: str
    version: str | None
    updated_at: str
    body: str


@router.get("/articles/{article_id}", response_model=ArticleResponse)
async def get_article(article_id: str) -> ArticleResponse:
    try:
        article_uuid = uuid.UUID(article_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="article not found") from exc

    def _lookup() -> KnowledgeArticle | None:
        with SessionLocal() as session:
            article = session.get(KnowledgeArticle, article_uuid)
            if article is not None and article.status != ArticleStatus.published:
                return None
            if article is not None:
                session.expunge(article)
            return article

    article = await asyncio.to_thread(_lookup)
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    return ArticleResponse(
        article_id=str(article.id),
        title=article.title,
        category=article.category,
        doc_type=article.doc_type,
        version=article.version,
        updated_at=article.updated_at.isoformat(),
        body=article.body,
    )

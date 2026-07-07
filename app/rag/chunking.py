"""Split knowledge articles into overlapping chunks sized for embedding and retrieval.

Strategy (tuned against `make eval`, see ADR-016):
- Heading-aware first: the body is split into markdown sections (a heading line plus its text),
  and whole sections are greedily packed into chunks of at most CHUNK_TOKENS. Headings are
  natural topic boundaries in the KB articles, so packing never severs a heading from its text.
- Token-window fallback: a single section longer than CHUNK_TOKENS is split into windows of
  CHUNK_TOKENS with OVERLAP_TOKENS of overlap.
- Continuity: every chunk after the first is prefixed with the last OVERLAP_TOKENS of the
  previous chunk plus a `# <title> (continued)` header line. The header keeps the article's
  key terms (usually only present in the H1 of chunk 0) visible to BOTH halves of hybrid
  search for continuation chunks — without it, "reset my password" can't lexically match
  chunk 1 of the password-reset article.

Every chunk carries the ADR-013 denormalized metadata (category, doc_type, status, version)
copied from the parent article so retrieval can pre-filter on the chunk row itself.

Token counts use tiktoken's cl100k_base — the tokenizer of text-embedding-3-small, i.e. we
measure size in the units the embedding model actually sees.
"""
# Implemented in M1.

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50

_HEADING_RE = re.compile(r"^#{1,6}\s", flags=re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit; mirrors the article_chunks columns the ingest pipeline writes."""

    article_id: uuid.UUID
    chunk_index: int
    content: str
    category: str
    doc_type: str
    status: str
    version: str | None


@lru_cache
def _enc() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc().encode(text))


def _split_sections(body: str) -> list[str]:
    """Split on markdown headings; each section is a heading line plus everything until the next."""
    starts = [m.start() for m in _HEADING_RE.finditer(body)]
    if not starts:
        return [body] if body.strip() else []
    sections = []
    if body[: starts[0]].strip():  # preamble before the first heading
        sections.append(body[: starts[0]])
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        sections.append(body[start:end])
    return [s.strip() for s in sections if s.strip()]


def _window_split(text: str, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    """Token-window fallback for a single oversized section."""
    tokens = _enc().encode(text)
    step = chunk_tokens - overlap_tokens
    return [
        _enc().decode(tokens[i : i + chunk_tokens]).strip()
        for i in range(0, len(tokens), step)
        if tokens[i : i + chunk_tokens]
    ]


def _overlap_tail(text: str, overlap_tokens: int) -> str:
    tokens = _enc().encode(text)
    return _enc().decode(tokens[-overlap_tokens:]).strip()


def chunk_text(
    body: str,
    *,
    title: str | None = None,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[str]:
    """Heading-aware packing into ~chunk_tokens chunks with overlap_tokens continuity. Pure text in/out."""
    if not body.strip():
        return []

    # 1. Sections, with oversized ones pre-split by token window.
    pieces: list[str] = []
    for section in _split_sections(body):
        if count_tokens(section) > chunk_tokens:
            pieces.extend(_window_split(section, chunk_tokens, overlap_tokens))
        else:
            pieces.append(section)

    # 2. Greedy packing of consecutive pieces.
    packed: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for piece in pieces:
        piece_tokens = count_tokens(piece)
        if current and current_tokens + piece_tokens > chunk_tokens:
            packed.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(piece)
        current_tokens += piece_tokens
    if current:
        packed.append("\n\n".join(current))

    # 3. Continuity prefix for chunks after the first: title header + overlap tail.
    chunks = [packed[0]]
    for prev, chunk in zip(packed, packed[1:]):
        prefix = f"# {title} (continued)\n\n" if title else ""
        tail = _overlap_tail(prev, overlap_tokens)
        chunks.append(f"{prefix}[…] {tail}\n\n{chunk}")
    return chunks


def chunk_article(
    *,
    article_id: uuid.UUID,
    title: str,
    body: str,
    category: str,
    doc_type: str,
    status: str,
    version: str | None,
) -> list[Chunk]:
    """Chunk one article, stamping each chunk with the denormalized parent metadata (ADR-013)."""
    return [
        Chunk(
            article_id=article_id,
            chunk_index=i,
            content=content,
            category=category,
            doc_type=doc_type,
            status=status,
            version=version,
        )
        for i, content in enumerate(chunk_text(body, title=title))
    ]

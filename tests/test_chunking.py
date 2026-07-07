"""Unit tests for article chunking (sizes, overlap, edge cases)."""
# Implemented in M1.

from __future__ import annotations

import uuid

from app.rag.chunking import (
    CHUNK_TOKENS,
    OVERLAP_TOKENS,
    _overlap_tail,
    chunk_article,
    chunk_text,
    count_tokens,
)


def make_section(heading: str, approx_tokens: int) -> str:
    return f"## {heading}\n\n" + ("word " * approx_tokens)


def test_short_body_single_chunk():
    body = "# Title\n\nA short how-to."
    chunks = chunk_text(body, title="Title")
    assert chunks == [body]


def test_empty_body_no_chunks():
    assert chunk_text("", title="T") == []
    assert chunk_text("   \n  ", title="T") == []


def test_chunks_respect_token_budget():
    body = "\n\n".join(make_section(f"S{i}", 200) for i in range(8))
    chunks = chunk_text(body, title="Big article")
    assert len(chunks) > 1
    # continuation chunks carry the title header + overlap tail on top of the packed budget
    slack = OVERLAP_TOKENS + 20
    assert all(count_tokens(c) <= CHUNK_TOKENS + slack for c in chunks)


def test_overlap_carries_previous_tail():
    body = "\n\n".join(make_section(f"S{i}", 300) for i in range(3))
    chunks = chunk_text(body, title="T")
    assert len(chunks) >= 2
    for prev, cur in zip(chunks, chunks[1:]):
        # the tail is recomputed over the raw previous chunk; the continuation marker `[…]`
        # precedes it in the current chunk
        tail = _overlap_tail(prev, OVERLAP_TOKENS)
        assert tail[-40:] in cur


def test_heading_not_severed_from_its_section():
    body = make_section("Alpha", 300) + "\n\n" + make_section("Beta", 300)
    chunks = chunk_text(body, title="T")
    assert len(chunks) == 2
    assert "## Alpha" in chunks[0] and "## Beta" not in chunks[0]
    assert "## Beta" in chunks[1]


def test_continuation_chunks_carry_title_header():
    body = "\n\n".join(make_section(f"S{i}", 300) for i in range(3))
    chunks = chunk_text(body, title="VPN setup guide")
    assert not chunks[0].startswith("# VPN setup guide (continued)")
    assert all(c.startswith("# VPN setup guide (continued)") for c in chunks[1:])


def test_oversized_single_section_window_split():
    body = "## Only\n\n" + ("token " * (CHUNK_TOKENS * 3))
    chunks = chunk_text(body, title="T")
    assert len(chunks) >= 3


def test_chunking_is_deterministic():
    body = "\n\n".join(make_section(f"S{i}", 250) for i in range(4))
    assert chunk_text(body, title="T") == chunk_text(body, title="T")


def test_chunk_article_stamps_metadata():
    article_id = uuid.uuid4()
    chunks = chunk_article(
        article_id=article_id,
        title="T",
        body="\n\n".join(make_section(f"S{i}", 300) for i in range(3)),
        category="software",
        doc_type="howto",
        status="published",
        version="v5.1",
    )
    assert len(chunks) >= 2
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(
        (c.article_id, c.category, c.doc_type, c.status, c.version)
        == (article_id, "software", "howto", "published", "v5.1")
        for c in chunks
    )

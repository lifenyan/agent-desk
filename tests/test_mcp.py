"""MCP server tests (M8, ADR-040) — LLM-free; the DB-backed ones skip when Postgres is down.

What's pinned here:
- the exposed tool set is EXACTLY the four read/create tools — the MCP surface must never
  silently grow an approval or update capability (test_agents.test_no_agent_can_approve_orders
  precedent, applied to the second adapter);
- bearer-token auth: the static map parses strictly, the verifier accepts/rejects, and the
  verified token's user is what the plain tools act as (identity threading end-to-end minus
  HTTP — the live transport is exercised by ignore/tem/m8_mcp_smoke.py against a real client).
"""
# Implemented in M8.

from __future__ import annotations

import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()

from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402

import mcp_server.server as ms  # noqa: E402
from tests.conftest import requires_db  # noqa: E402

DEMO_EMAIL = "demo.user@corp.com"


def test_token_map_parses_and_rejects_malformed():
    assert ms.parse_token_map("") == {}
    assert ms.parse_token_map("abc=a@b.c, xyz=d@e.f ,") == {"abc": "a@b.c", "xyz": "d@e.f"}
    for bad in ("justatoken", "=email@only", "token=", "a=b,junk"):
        with pytest.raises(ValueError, match="malformed"):
            ms.parse_token_map(bad)


async def test_verifier_maps_token_to_subject_email():
    verifier = ms.StaticTokenVerifier({"tok-1": DEMO_EMAIL})
    access = await verifier.verify_token("tok-1")
    assert access is not None and access.subject == DEMO_EMAIL
    assert await verifier.verify_token("wrong") is None
    assert await verifier.verify_token("") is None


async def test_exposed_tools_are_exactly_the_read_create_surface():
    names = {t.name for t in await ms.mcp.list_tools()}
    assert names == {
        "search_knowledge_articles",
        "list_catalog_items",
        "create_ticket",
        "get_ticket_status",
    }
    # ADR-005/020 discipline on the second adapter: approval authority and ticket mutation
    # stay off the external surface entirely.
    assert not names & {"approve_order", "reject_order", "update_ticket", "place_catalog_order"}


@pytest.fixture
def authed_as_demo():
    """What BearerAuthBackend + AuthContextMiddleware do per request, minus HTTP."""
    from mcp.server.auth.provider import AccessToken

    user = AuthenticatedUser(
        AccessToken(token="tok", client_id=DEMO_EMAIL, scopes=[], subject=DEMO_EMAIL)
    )
    token = auth_context_var.set(user)
    yield
    auth_context_var.reset(token)


@requires_db
def test_acting_context_threads_verified_identity(authed_as_demo):
    # Past the resolve_acting_user gate (identity found) into the ownership guards: a random
    # UUID must yield "not found", never an identity error — proving the bearer token's user
    # is who the plain tool acted as.
    payload = ms.get_ticket_status(str(uuid.uuid4()))
    assert "not found" in payload["error"]


@requires_db
def test_no_auth_context_means_no_acting_user():
    payload = ms.get_ticket_status(str(uuid.uuid4()))
    assert "no acting user" in payload["error"]

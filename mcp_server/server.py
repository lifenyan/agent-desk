"""MCP server (M8, ADR-040): the same plain tool layer, exposed over the Model Context Protocol.

One tool surface, two adapters: app/tools/* holds PLAIN functions; the Agents SDK path wraps
them with @function_tool for the chat agents, and this server wraps the SAME functions as MCP
tools for external clients (Claude Desktop & co). No logic lives in either wrapper — schemas
come from the shared signatures/docstrings, and every guard (identity, parse → exists →
ownership, enum checks) runs in the plain layer regardless of who is calling (ADR-004).

Identity (ADR-039, the user_tools DESIGN NOTE applied to MCP): a static bearer token maps to
ONE seeded acting user via settings.mcp_tokens ("token=email,…"). The mapped email goes into
ChatContext.user_id exactly the way routes_chat builds it, and resolve_acting_user inside the
tools does the trusting — an MCP client can no more name another user than the LLM can. Full
multi-user auth (OAuth flows, token issuance/rotation, scopes) is deliberately OUT OF SCOPE:
this demonstrates the boundary, not an identity product.

Transport: streamable HTTP (stateless — every tool call carries its own Authorization header,
verified per-request by the SDK's BearerAuthBackend; the verified token reaches tools through
the SDK's own contextvar). Run: `make mcp`, then connect per README "MCP server" section.
"""
# Implemented in M8.

from __future__ import annotations

from dotenv import load_dotenv

# Same bootstrap as app.main: .env before anything reads os.environ (DB URL, OPENAI_API_KEY).
load_dotenv()

from agents import RunContextWrapper  # noqa: E402
from mcp.server.auth.middleware.auth_context import get_access_token  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402
from mcp.server.auth.settings import AuthSettings  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import AnyHttpUrl  # noqa: E402

from app.agents.context import ChatContext  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import OS, TicketCategory, TicketPriority, TicketType  # noqa: E402
from app.tools import catalog_tools, knowledge_tools, ticket_tools  # noqa: E402


def parse_token_map(raw: str) -> dict[str, str]:
    """settings.mcp_tokens ("token=email[,token=email…]") → {token: acting-user email}."""
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        token, sep, email = pair.partition("=")
        if not sep or not token.strip() or not email.strip():
            raise ValueError(f"malformed MCP_TOKENS entry {pair!r}: expected 'token=email'")
        tokens[token.strip()] = email.strip()
    return tokens


class StaticTokenVerifier:
    """SDK TokenVerifier over the static env map: valid token → AccessToken carrying the
    mapped user email in `subject`; anything else → None (the SDK answers 401)."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        email = self._tokens.get(token)
        if email is None:
            return None
        return AccessToken(token=token, client_id=email, scopes=[], subject=email)


def _acting_context() -> RunContextWrapper[ChatContext]:
    """The trusted run context for the plain tools — built from the VERIFIED bearer token,
    exactly the shape routes_chat builds from ChatRequest.user_id. No second identity path."""
    access = get_access_token()
    email = access.subject if access is not None else None
    return RunContextWrapper(context=ChatContext(user_id=email, source="mcp"))


settings = get_settings()
_base = f"http://localhost:{settings.mcp_port}"

mcp = FastMCP(
    "agentdesk",
    instructions=(
        "IT service desk tools: search the knowledge base, browse the catalog, and create or "
        "check support tickets. Tickets are created for the user your token maps to."
    ),
    host="127.0.0.1",
    port=settings.mcp_port,
    stateless_http=True,  # per-request auth: each call carries (and is verified by) its token
    token_verifier=StaticTokenVerifier(parse_token_map(settings.mcp_tokens)),
    auth=AuthSettings(
        # Nominal RFC 9728 metadata (the SDK requires it when auth is on). There is no OAuth
        # issuer — tokens are static (see module docstring); clients just send the header.
        issuer_url=AnyHttpUrl(_base),
        resource_server_url=AnyHttpUrl(f"{_base}/mcp"),
    ),
)


@mcp.tool()
def search_knowledge_articles(
    query: str, category: TicketCategory | None = None, version: str | None = None
) -> dict:
    """Search the IT knowledge base with hybrid (semantic + keyword) retrieval.

    Args:
        query: The search query — key terms, rephrased into likely knowledge-base wording.
        category: Optional category filter (accounts, software, hardware, network, email, other).
        version: Optional product version filter, e.g. "v5.1".
    """
    return knowledge_tools.search_knowledge_articles(query, category=category, version=version)


@mcp.tool()
def list_catalog_items(os_filter: OS | None = None) -> dict:
    """List orderable catalog items (hardware, software licenses, services) with price and
    order-form schema.

    Args:
        os_filter: Only return items compatible with this OS (macos, windows, linux).
    """
    return catalog_tools.list_catalog_items(os_filter=os_filter)


@mcp.tool()
def create_ticket(
    title: str,
    description: str,
    category: TicketCategory,
    priority: TicketPriority = TicketPriority.medium,
    type: TicketType = TicketType.incident,
) -> dict:
    """Create a support ticket for the authenticated user.

    Args:
        title: Short summary of the issue, e.g. "VPN drops every 30 minutes".
        description: Full description: what happens, what was tried, error messages.
        category: One of: accounts, software, hardware, network, email, other.
        priority: Business impact (low, medium, high, critical). Default medium.
        type: "incident" (something is broken, default) or "request".
    """
    # asset_id is deliberately not exposed: get_user_assets isn't an MCP tool, so a client
    # has no legitimate way to obtain one (the plain layer would reject a guess anyway).
    return ticket_tools.create_ticket(
        _acting_context(), title, description, category, priority=priority, type=type
    )


@mcp.tool()
def get_ticket_status(ticket_id: str) -> dict:
    """Get the status of one of the authenticated user's own tickets: status, priority,
    category, and the latest support comment.

    Args:
        ticket_id: UUID of the ticket (as returned by create_ticket).
    """
    return ticket_tools.get_ticket_status(_acting_context(), ticket_id)


if __name__ == "__main__":
    if not settings.mcp_tokens:
        raise SystemExit(
            "MCP_TOKENS is not set — configure at least one 'token=email' pair (see README)"
        )
    mcp.run(transport="streamable-http")

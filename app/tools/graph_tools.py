"""Graph tool: query_dependency_graph — CMDB impact / root-cause traversal (M9, ADR-035/037).

The ONLY agent-facing surface of the dependency graph (ADR-004: tools are the only DB access
path — the graph tool is a tool like any other). Plain function first, then the
@function_tool wrapper, house style.

Division of labor: this module resolves the entity NAME to a CI (against Postgres — the
source of truth regardless of traversal backend), delegates the walk to the configured
backend (graph/, settings.graph_backend), and enriches the result with the impacted-team ->
user-count rollup. Keeping resolution + enrichment here means the two backends stay pure
traversal — which is also what makes the eval's CTE-vs-Cypher latency comparison apples to
apples.

Identity note: the graph is shared infrastructure, deliberately user-independent — so unlike
the ticket/catalog tools there is no ctx/acting-user parameter (knowledge_tools precedent).
"""
# Implemented in M9.

from __future__ import annotations

import enum

from agents import function_tool
from sqlalchemy import func, select

from app.db.database import SessionLocal
from app.db.models import CI, CIType, User
from app.tools.user_tools import enum_error
from graph import get_graph_backend

MAX_DEPTH_LIMIT = 10  # the seeded graph is ~5 hops deep; 10 bounds runaway walks, not usage


class GraphDirection(enum.StrEnum):
    dependents = "dependents"  # impact: everything that (transitively) relies on the entity
    dependencies = "dependencies"  # root cause: everything the entity (transitively) relies on


def query_dependency_graph(
    entity: str,
    direction: GraphDirection = GraphDirection.dependents,
    max_depth: int = 5,
) -> dict:
    """Traverse the CMDB dependency graph from a named infrastructure CI (service, server,
    database, or team).

    Use direction="dependents" for impact analysis ("server X is down — what breaks, which
    teams/users are affected?") and direction="dependencies" for root-cause analysis ("service
    Y is failing — what does it rely on that could explain it?"). For a shared root cause
    across several failing services, query dependencies for each and intersect the results.

    Args:
        entity: CI name, e.g. "auth-service", "db-server-02", "crm-db". Names, never UUIDs.
        direction: "dependents" (impact, default) or "dependencies" (root cause).
        max_depth: Maximum hops to walk (default 5, limit 10). The default covers the whole
            seeded graph; lower it to see only close neighbors.
    """
    if error := enum_error(direction, GraphDirection, "direction"):
        return error
    if not isinstance(max_depth, int) or not 1 <= max_depth <= MAX_DEPTH_LIMIT:
        return {"error": f"invalid max_depth {max_depth!r}: expected 1..{MAX_DEPTH_LIMIT}"}

    with SessionLocal() as session:
        ci = session.scalar(select(CI).where(func.lower(CI.name) == entity.strip().lower()))
        if ci is None:
            candidates = session.scalars(
                select(CI.name).where(CI.name.ilike(f"%{entity.strip()}%")).limit(5)
            ).all()
            hint = f" — did you mean one of {sorted(candidates)}?" if candidates else ""
            return {"error": f"no CI named {entity!r} in the CMDB{hint}"}

        nodes = get_graph_backend()(str(ci.id), str(direction), max_depth)

        # Teams-as-nodes (ADR-035) make the user rollup a lookup, not a second traversal.
        impacted_teams = [
            {
                "team": n["name"],
                "org": n["owner_org"],
                "user_count": session.scalar(
                    select(func.count()).select_from(User).where(User.org == n["owner_org"])
                ),
            }
            for n in nodes
            if n["ci_type"] == CIType.team
        ]

    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["ci_type"]] = counts.get(n["ci_type"], 0) + 1
    return {
        "entity": {"name": ci.name, "ci_type": ci.ci_type, "owner_org": ci.owner_org},
        "direction": str(direction),
        "max_depth": max_depth,
        "guidance": (
            "nodes is the COMPLETE transitive set within max_depth, nearest first (depth = "
            "shortest hop distance). A CI not listed is not affected via the dependency "
            "graph. depth>1 nodes are indirect: impacted/relied on through the depth-1 nodes."
        ),
        "nodes": nodes,
        "counts_by_type": counts,
        "impacted_teams": impacted_teams,
    }


# --- Agents SDK wrapper (schema derived from the signature + docstring above) ---
query_dependency_graph_tool = function_tool(query_dependency_graph)

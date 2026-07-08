"""CMDB dependency-graph traversal (M9): one query contract, two interchangeable backends.

Both backends answer the same question — "starting from this CI, what transitively depends on
it (impact) or what does it transitively depend on (root cause)?" — and return the identical
payload shape, so `app/tools/graph_tools.py` (the only agent-facing surface, ADR-004) and the
eval suite can flip between them with a config switch:

- `graph/postgres_graph.py` — recursive CTE over the `dependencies` table (the default; no
  extra infrastructure).
- `graph/neo4j_graph.py` — Cypher over a Neo4j projection kept in sync by
  `graph/sync_neo4j.py` (Postgres stays the source of truth; Neo4j is a derived copy).

Backend contract: `query(ci_id, direction, max_depth) -> list[dict]` where each node dict is
`{"name", "ci_type", "owner_org", "depth"}` (depth = SHORTEST hop distance from the start CI),
sorted by (depth, name), start CI excluded. Selection: `settings.graph_backend` — "postgres"
(default) or "neo4j". A missing Neo4j must fail LOUDLY on the neo4j path and must not affect
the postgres path at all (ADR-037).
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import get_settings

QueryFn = Callable[[str, str, int], list[dict]]


def get_graph_backend() -> QueryFn:
    """Resolve the traversal backend from settings. Import inside the function: the neo4j
    driver must never be a prerequisite for the postgres path."""
    backend = get_settings().graph_backend
    if backend == "postgres":
        from graph.postgres_graph import query as pg_query

        return pg_query
    if backend == "neo4j":
        from graph.neo4j_graph import query as neo_query

        return neo_query
    raise ValueError(f"unknown graph backend {backend!r}: expected 'postgres' or 'neo4j'")

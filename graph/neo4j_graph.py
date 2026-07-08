"""Neo4j traversal backend: the same contract as postgres_graph, over Cypher (M9 Phase 2).

Projection model (written by graph/sync_neo4j.py — Postgres stays the source of truth):
- (:CI {id, name, ci_type, owner_org}) nodes, unique on id;
- (dependent)-[:DEPENDS_ON {dep_type}]->(dependency) edges, matching the `dependencies` rows.

The traversal is one variable-length pattern match; direction is which way the arrow points
relative to the start node. Cycle safety is free: Cypher's relationship isomorphism forbids a
path from using the same relationship twice, so cyclic paths terminate without an explicit
path guard (the CTE needs one — half the code-complexity story in ADR-037). `min(length(p))`
collapses multi-path nodes to shortest hop distance, mirroring the CTE's GROUP BY/MIN.

Failure mode (ADR-037): anything wrong with Neo4j — driver missing, server down, wrong
credentials — RAISES out of query(). There is deliberately no fallback to postgres here: a
silent fallback would make the config switch a lie and the eval comparison meaningless.
`ping()` exists for tests/evals to SKIP gracefully; the tool path never calls it.
"""

from __future__ import annotations

from app.config import get_settings

_driver_singleton = None


def _get_driver():
    """Lazy module-level driver (its connection pool is what makes the tool-level latency
    comparison fair against SessionLocal's pooled Postgres connections)."""
    global _driver_singleton
    if _driver_singleton is None:
        from neo4j import GraphDatabase  # lazy: never a prerequisite for the postgres path

        settings = get_settings()
        _driver_singleton = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
    return _driver_singleton


def ping() -> bool:
    """Availability probe for tests/evals (skip, don't fail). The query path never uses it."""
    try:
        _get_driver().verify_connectivity()
        return True
    except Exception:  # noqa: BLE001 — driver missing or server down: same answer for a probe
        return False


# {arrow_left}/{arrow_right} orient the pattern; the *1..{depth} bound must be a literal in
# Cypher (validated int from the tool layer, so the interpolation is safe).
# `n <> start`: relationship isomorphism terminates cyclic walks but still RETURNS the start
# node when a cycle closes back on it (a->b->c->a is three distinct relationships) — the
# CTE's path guard never does. Found by the cycle-parity test, kept aligned explicitly.
_WALK_CYPHER = """
MATCH p = (start:CI {{id: $id}}){arrow_left}[:DEPENDS_ON*1..{depth}]{arrow_right}(n:CI)
WHERE n <> start
RETURN n.name AS name, n.ci_type AS ci_type, n.owner_org AS owner_org,
       min(length(p)) AS depth
ORDER BY depth, name
"""

_ARROWS = {
    "dependents": ("<-", "-"),  # who depends on start: arrows point AT start
    "dependencies": ("-", "->"),  # what start depends on: arrows point away
}


def query(ci_id: str, direction: str, max_depth: int) -> list[dict]:
    """Traverse from `ci_id`; see the package docstring for the shared backend contract."""
    arrow_left, arrow_right = _ARROWS[direction]
    cypher = _WALK_CYPHER.format(arrow_left=arrow_left, arrow_right=arrow_right, depth=max_depth)
    with _get_driver().session() as session:
        return [
            {
                "name": r["name"],
                "ci_type": r["ci_type"],
                "owner_org": r["owner_org"],
                "depth": int(r["depth"]),
            }
            for r in session.run(cypher, id=ci_id)
        ]

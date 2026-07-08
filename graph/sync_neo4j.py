"""Postgres -> Neo4j projection sync (M9 Phase 2): `python -m graph.sync_neo4j`.

Postgres is the source of truth for the CMDB (the graph tool resolves names there and seeding
writes only there); Neo4j holds a DERIVED projection for the Cypher backend. This sync is
idempotent and convergent:
- MERGE every CI node by id and overwrite its properties (renames/re-typing propagate);
- MERGE every DEPENDS_ON relationship by (dependent, dependency) pair;
- then DELETE any node or relationship that no longer exists in Postgres (so re-running after
  edits never leaves ghosts — MERGE alone only ever adds).

Run it after (re)seeding, or whenever data/dependencies.json changes. Requires the compose
neo4j service: `docker compose up -d neo4j`. Fails loudly if Neo4j is unreachable.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import CI, Dependency
from graph.neo4j_graph import _get_driver


def sync() -> dict:
    with SessionLocal() as s:
        nodes = [
            {
                "id": str(c.id),
                "name": c.name,
                "ci_type": c.ci_type,
                "owner_org": c.owner_org,
            }
            for c in s.scalars(select(CI))
        ]
        edges = [
            {
                "dependent": str(d.dependent_id),
                "dependency": str(d.dependency_id),
                "dep_type": d.dep_type,
            }
            for d in s.scalars(select(Dependency))
        ]

    driver = _get_driver()
    driver.verify_connectivity()  # loud, early
    with driver.session() as session:
        session.run("CREATE CONSTRAINT ci_id IF NOT EXISTS FOR (c:CI) REQUIRE c.id IS UNIQUE")
        session.run(
            """
            UNWIND $nodes AS row
            MERGE (c:CI {id: row.id})
            SET c.name = row.name, c.ci_type = row.ci_type, c.owner_org = row.owner_org
            """,
            nodes=nodes,
        )
        session.run(
            """
            UNWIND $edges AS row
            MATCH (a:CI {id: row.dependent}), (b:CI {id: row.dependency})
            MERGE (a)-[r:DEPENDS_ON]->(b)
            SET r.dep_type = row.dep_type
            """,
            edges=edges,
        )
        # Convergence: drop anything Postgres no longer has.
        stale_rels = session.run(
            """
            MATCH (a:CI)-[r:DEPENDS_ON]->(b:CI)
            WHERE NOT a.id + '|' + b.id IN $pairs
            DELETE r
            RETURN count(r) AS n
            """,
            pairs=[f"{e['dependent']}|{e['dependency']}" for e in edges],
        ).single()["n"]
        stale_nodes = session.run(
            "MATCH (n:CI) WHERE NOT n.id IN $ids DETACH DELETE n RETURN count(n) AS n",
            ids=[n["id"] for n in nodes],
        ).single()["n"]
        counts = session.run(
            "MATCH (n:CI) OPTIONAL MATCH (n)-[r:DEPENDS_ON]->() "
            "RETURN count(DISTINCT n) AS nodes, count(r) AS edges"
        ).single()

    return {
        "nodes_synced": len(nodes),
        "edges_synced": len(edges),
        "stale_nodes_deleted": stale_nodes,
        "stale_edges_deleted": stale_rels,
        "neo4j_now": {"nodes": counts["nodes"], "edges": counts["edges"]},
    }


if __name__ == "__main__":
    print(f"sync complete: {sync()}")

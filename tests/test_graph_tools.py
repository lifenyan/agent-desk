"""LLM-free tests for the M9 CMDB graph: traversal backends + the query_dependency_graph tool.

Three layers, mirroring the module split:
- backend contract against the SEEDED graph (exact impacted sets — the committed taxonomy is
  deterministic ground truth, so exact-set assertions are cheap and catch silent edge drift);
- backend contract against a SYNTHETIC temp subgraph (cycle safety, depth/direction
  semantics, diamond -> shortest-depth dedup) — created and deleted per test;
- the tool payload contract (name resolution, guards, team/user rollup, no bare "results"
  key — the _collect_citations leak rule).

Both backends run the same seeded/parity assertions: postgres always; neo4j is skipped
unless the server is reachable (CI has no Neo4j service — the postgres path must be fully
green without it, ADR-037). The neo4j cycle test creates its temp nodes directly in Neo4j
(the sync only projects Postgres, and the cycle rows are transient test data).
"""
# Implemented in M9.

from __future__ import annotations

import uuid

import pytest

from app.db.database import SessionLocal
from app.db.models import CI, Dependency
from tests.conftest import requires_db

# ---------------------------------------------------------------------------------------------
# Backend availability
# ---------------------------------------------------------------------------------------------


def _neo4j_available() -> bool:
    try:
        from graph.neo4j_graph import ping

        return ping()
    except Exception:  # noqa: BLE001 — driver missing or server down: same answer
        return False


_NEO4J = _neo4j_available()

requires_neo4j = pytest.mark.skipif(
    not _NEO4J, reason="Neo4j not reachable (docker compose up neo4j + python -m graph.sync_neo4j)"
)

BACKENDS = [
    pytest.param("postgres", marks=requires_db),
    pytest.param("neo4j", marks=[requires_db, requires_neo4j]),
]


def _query(backend: str):
    if backend == "postgres":
        from graph.postgres_graph import query
    else:
        from graph.neo4j_graph import query
    return query


def _ci_id(name: str) -> str:
    with SessionLocal() as s:
        return str(s.query(CI).filter(CI.name == name).one().id)


# ---------------------------------------------------------------------------------------------
# Seeded-graph contract (exact sets from the committed taxonomy plan)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_db_server_01_impact_is_the_full_auth_cascade(backend):
    """The flagship shared node: db-server-01 hosts auth-db AND ldap-db, so its dependents
    are 14 of the 15 services + all 4 teams — and never monitoring (the isolation control)."""
    nodes = _query(backend)(_ci_id("db-server-01"), "dependents", 10)
    names = {n["name"] for n in nodes}
    assert {n["name"] for n in nodes if n["ci_type"] == "database"} == {"auth-db", "ldap-db"}
    assert {n["name"] for n in nodes if n["ci_type"] == "team"} == {
        "team-sales",
        "team-engineering",
        "team-finance",
        "team-hr",
    }
    assert "monitoring-service" not in names and "monitoring-db" not in names
    assert len([n for n in nodes if n["ci_type"] == "service"]) == 14


@pytest.mark.parametrize("backend", BACKENDS)
def test_crm_service_outage_does_not_reach_crm_db_readers(backend):
    """Trap case: finance-reporting and sales-dashboard read crm-db DIRECTLY — an outage of
    crm-service (the API) must not claim finance-reporting as impacted."""
    nodes = _query(backend)(_ci_id("crm-service"), "dependents", 10)
    assert {n["name"] for n in nodes} == {"sales-dashboard", "team-sales"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_dependencies_direction_walks_with_the_arrows(backend):
    nodes = _query(backend)(_ci_id("sso-gateway"), "dependencies", 10)
    by_name = {n["name"]: n["depth"] for n in nodes}
    # sso-gateway -> auth-service -> ldap-directory -> ldap-db -> db-server-01
    assert by_name["auth-service"] == 1
    assert by_name["ldap-directory"] == 2
    assert by_name["ldap-db"] == 3
    # db-server-01 is reachable at depth 4 via ldap-db AND at depth 3 via auth-db (auth-db is
    # hosted on it too) — the contract is SHORTEST hop distance.
    assert by_name["db-server-01"] == 3
    # nothing that DEPENDS ON sso-gateway may appear when walking its dependencies
    assert "hr-portal" not in by_name and "ticketing-service" not in by_name


@pytest.mark.parametrize("backend", BACKENDS)
def test_depth_limits_the_walk(backend):
    ci = _ci_id("db-server-01")
    q = _query(backend)
    depth1 = {n["name"] for n in q(ci, "dependents", 1)}
    assert depth1 == {"auth-db", "ldap-db"}
    depth2 = {n["name"] for n in q(ci, "dependents", 2)}
    assert depth2 == depth1 | {"auth-service", "ldap-directory"}
    # depths reported are shortest-hop and never exceed the cap
    assert all(n["depth"] <= 2 for n in q(ci, "dependents", 2))


@pytest.mark.parametrize("backend", BACKENDS)
def test_leaf_and_isolated_nodes(backend):
    q = _query(backend)
    # teams depend on things but nothing depends on a team
    assert q(_ci_id("team-sales"), "dependents", 10) == []
    # servers are pure dependencies: they depend on nothing
    assert q(_ci_id("db-server-01"), "dependencies", 10) == []


@requires_db
@requires_neo4j
def test_backends_agree_on_every_seeded_ci():
    """Parity: the Cypher projection must return the identical (name, depth) sets as the CTE
    for every seeded CI, both directions — the eval's three-way comparison rests on this."""
    from graph.neo4j_graph import query as neo_q
    from graph.postgres_graph import query as pg_q

    with SessionLocal() as s:
        cis = [(str(c.id), c.name) for c in s.query(CI).all()]
    for ci_id, name in cis:
        for direction in ("dependents", "dependencies"):
            pg = {(n["name"], n["depth"]) for n in pg_q(ci_id, direction, 10)}
            neo = {(n["name"], n["depth"]) for n in neo_q(ci_id, direction, 10)}
            assert pg == neo, f"backend mismatch for {name} {direction}: {pg ^ neo}"


# ---------------------------------------------------------------------------------------------
# Synthetic subgraph: cycle safety + diamond dedup
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def cycle_subgraph():
    """Temp 3-cycle a->b->c->a (calls edges; 'X calls Y' = X depends on Y) plus a diamond
    d -> {a, b} so one node is reachable at two depths. Deleted afterwards (edge CASCADE)."""
    names = {k: f"test-cycle-{k}-{uuid.uuid4().hex[:8]}" for k in "abcd"}
    ids: dict[str, str] = {}
    with SessionLocal() as s:
        for k, name in names.items():
            ci = CI(name=name, ci_type="service", owner_org=None)
            s.add(ci)
            s.flush()
            ids[k] = str(ci.id)
        for dependent, dependency in [("a", "b"), ("b", "c"), ("c", "a"), ("d", "a"), ("d", "b")]:
            s.add(
                Dependency(
                    dependent_id=ids[dependent], dependency_id=ids[dependency], dep_type="calls"
                )
            )
        s.commit()
    try:
        yield names, ids
    finally:
        with SessionLocal() as s:
            s.query(CI).filter(CI.id.in_(list(ids.values()))).delete(synchronize_session=False)
            s.commit()


@requires_db
def test_cycle_terminates_and_returns_each_node_once(cycle_subgraph):
    from graph.postgres_graph import query

    names, ids = cycle_subgraph
    # dependencies of a: a->b (1), b->c (2), c->a stops (a is on the path) — no infinite walk
    nodes = query(ids["a"], "dependencies", 10)
    assert [(n["name"], n["depth"]) for n in nodes] == [(names["b"], 1), (names["c"], 2)]
    # dependents of a: c and d directly (1), b through c (2) — and the c->a edge never loops
    nodes = query(ids["a"], "dependents", 10)
    assert {n["name"] for n in nodes} == {names["b"], names["c"], names["d"]}
    assert all(nodes.count(n) == 1 for n in nodes), "each node exactly once despite the cycle"


@requires_db
def test_diamond_reports_shortest_depth(cycle_subgraph):
    from graph.postgres_graph import query

    names, ids = cycle_subgraph
    # d depends on both a (depth 1) and b (depth 1); walking d's dependencies, c is reachable
    # via b at depth 2 and via a->b at depth 3 — shortest must win.
    by_name = {n["name"]: n["depth"] for n in query(ids["d"], "dependencies", 10)}
    assert by_name[names["a"]] == 1
    assert by_name[names["b"]] == 1
    assert by_name[names["c"]] == 2


@requires_neo4j
def test_neo4j_cycle_terminates():
    """Same cycle shape, created directly in Neo4j (transient test nodes — the sync projects
    Postgres and must not see these)."""
    from graph.neo4j_graph import _get_driver, query

    tag = uuid.uuid4().hex[:8]
    ids = {k: str(uuid.uuid4()) for k in "abc"}
    names = {k: f"test-cycle-{k}-{tag}" for k in "abc"}
    driver = _get_driver()  # module-level singleton: not ours to close
    with driver.session() as session:
        for k in "abc":
            session.run(
                "CREATE (:CI {id: $id, name: $name, ci_type: 'service'})",
                id=ids[k],
                name=names[k],
            )
        for dep, on in [("a", "b"), ("b", "c"), ("c", "a")]:
            session.run(
                "MATCH (x:CI {id: $x}), (y:CI {id: $y}) CREATE (x)-[:DEPENDS_ON]->(y)",
                x=ids[dep],
                y=ids[on],
            )
    try:
        nodes = query(ids["a"], "dependencies", 10)
        assert [(n["name"], n["depth"]) for n in nodes] == [
            (names["b"], 1),
            (names["c"], 2),
        ]
    finally:
        with driver.session() as session:
            session.run("MATCH (n:CI) WHERE n.id IN $ids DETACH DELETE n", ids=list(ids.values()))


# ---------------------------------------------------------------------------------------------
# Tool payload contract
# ---------------------------------------------------------------------------------------------


@requires_db
def test_tool_payload_contract():
    from app.tools.graph_tools import query_dependency_graph

    result = query_dependency_graph("db-server-08")
    assert "results" not in result, "bare 'results' key leaks into citations (house rule)"
    assert result["entity"] == {"name": "db-server-08", "ci_type": "server", "owner_org": None}
    names = {n["name"] for n in result["nodes"]}
    assert names == {
        "crm-db",
        "crm-service",
        "sales-dashboard",
        "finance-reporting",
        "team-sales",
        "team-finance",
    }
    teams = {t["team"]: t for t in result["impacted_teams"]}
    assert set(teams) == {"team-sales", "team-finance"}
    with SessionLocal() as s:
        from sqlalchemy import func, select

        from app.db.models import User

        sales_users = s.scalar(select(func.count()).select_from(User).where(User.org == "sales"))
    assert teams["team-sales"] == {"team": "team-sales", "org": "sales", "user_count": sales_users}
    assert result["counts_by_type"] == {"database": 1, "service": 3, "team": 2}


@requires_db
def test_tool_name_resolution_and_guards():
    from app.tools.graph_tools import query_dependency_graph as q

    assert q("AUTH-SERVICE")["entity"]["name"] == "auth-service"  # case-insensitive resolve
    assert "did you mean" in q("auth")["error"]  # ambiguous -> candidates, never auto-pick
    assert "no CI named" in q("plainly-not-a-ci")["error"]
    assert "invalid direction" in q("auth-service", "sideways")["error"]
    assert "invalid max_depth" in q("auth-service", "dependents", 0)["error"]
    assert "invalid max_depth" in q("auth-service", "dependents", 11)["error"]

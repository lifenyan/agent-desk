"""Postgres traversal backend: one recursive CTE over the `dependencies` edge table.

How the CTE walks the graph (LEARNING_M9 covers this in depth):
- The non-recursive term seeds the walk with the start CI's direct neighbors at depth 1.
- The recursive term joins `dependencies` against the previous frontier, growing depth by 1,
  until `max_depth` or no new rows.
- Cycle safety is the `path` array guard: a row is only expanded into nodes not already on
  the path that produced it. Postgres evaluates the recursive term breadth-first over the
  working table, so a diamond (two paths to the same node) yields two rows — the final
  GROUP BY ... MIN(depth) collapses them to the shortest hop distance.

Direction is which FK we anchor and which we expand:
- `dependents`  (impact):     anchor dependency_id = frontier, emit dependent_id — walking
  AGAINST the "depends on" arrows.
- `dependencies` (root cause): anchor dependent_id = frontier, emit dependency_id — walking
  WITH the arrows.
"""

from __future__ import annotations

from sqlalchemy import text as sql_text

from app.db.database import SessionLocal

# One template, two directions: {anchor} is the frontier side, {emit} the side we expand to.
_WALK_SQL = """
WITH RECURSIVE walk(ci_id, depth, path) AS (
    SELECT d.{emit}, 1, ARRAY[CAST(:start AS uuid), d.{emit}]
    FROM dependencies d
    WHERE d.{anchor} = CAST(:start AS uuid)
  UNION ALL
    SELECT d.{emit}, w.depth + 1, w.path || d.{emit}
    FROM dependencies d
    JOIN walk w ON d.{anchor} = w.ci_id
    WHERE w.depth < :max_depth
      AND NOT d.{emit} = ANY(w.path)
)
SELECT c.name, c.ci_type, c.owner_org, MIN(w.depth) AS depth
FROM walk w
JOIN cis c ON c.id = w.ci_id
GROUP BY c.name, c.ci_type, c.owner_org
ORDER BY depth, c.name
"""

_DIRECTIONS = {
    "dependents": _WALK_SQL.format(anchor="dependency_id", emit="dependent_id"),
    "dependencies": _WALK_SQL.format(anchor="dependent_id", emit="dependency_id"),
}


def query(ci_id: str, direction: str, max_depth: int) -> list[dict]:
    """Traverse from `ci_id`; see the package docstring for the shared backend contract."""
    sql = _DIRECTIONS[direction]
    with SessionLocal() as session:
        rows = session.execute(sql_text(sql), {"start": ci_id, "max_depth": max_depth}).mappings()
        return [
            {
                "name": r["name"],
                "ci_type": r["ci_type"],
                "owner_org": r["owner_org"],
                "depth": int(r["depth"]),
            }
            for r in rows
        ]

"""Health/readiness endpoints (DB + Redis connectivity) + cache hit/miss stats."""
# Implemented in M1; M3 added GET /cache/stats (the boring surface M6 exports to Langfuse).

from __future__ import annotations

import redis
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.cache import stats as cache_stats
from app.cache.redis_client import get_redis
from app.db.database import SessionLocal

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up. No dependencies checked (that's /readyz)."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(response: Response) -> dict:
    """Readiness: verify Postgres and Redis are reachable; 503 with per-dependency detail otherwise."""
    checks: dict[str, str] = {}

    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 — readiness must report, not raise
        checks["postgres"] = f"error: {exc.__class__.__name__}"

    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc.__class__.__name__}"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "unavailable", "checks": checks}


@router.get("/cache/stats")
def cache_stats_endpoint(response: Response) -> dict:
    """Persistent hit/miss counts + hit rates for the embedding/semantic/response caches."""
    try:
        return {"status": "ok", "caches": cache_stats.snapshot()}
    except redis.RedisError as exc:
        # Unlike cache reads/writes (which silently degrade), a stats READ has no request to
        # protect — report the outage explicitly instead of returning fake zeros.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "error": exc.__class__.__name__}

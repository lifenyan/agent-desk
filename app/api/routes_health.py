"""Health/readiness endpoints (DB + Redis connectivity)."""
# Implemented in M1.

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

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

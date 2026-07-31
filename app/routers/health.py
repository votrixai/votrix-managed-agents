"""Health probes used by Cloud Run and operators."""

from fastapi import APIRouter
from sqlalchemy import text

from app.routers.deps import Db


router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Process-level readiness; no external dependency is required."""
    return {"status": "ok"}


@router.get("/health/db", include_in_schema=False)
async def database_health(db: Db) -> dict[str, str]:
    """Prove the configured database accepts a round trip."""
    await db.execute(text("SELECT 1"))
    return {"status": "ok"}

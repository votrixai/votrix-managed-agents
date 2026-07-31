"""Health probes used by Cloud Run and operators."""

from time import monotonic

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.routers.deps import Db


router = APIRouter(tags=["health"])
_PROCESS_STARTED_AT = monotonic()
_GITHUB_REPOSITORY_URL = "https://github.com/votrixai/votrix-managed-agents"

HealthPayload = dict[str, str | float | None]


def _release_info() -> HealthPayload:
    """Describe the exact release and this individual process instance."""
    settings = get_settings()
    commit = settings.vma_git_commit_sha.strip()
    return {
        "status": "ok",
        "environment": settings.app_env,
        "build": settings.vma_public_build_id,
        "git_commit": commit or "unknown",
        "git_commit_url": f"{_GITHUB_REPOSITORY_URL}/commit/{commit}" if commit else None,
        "uptime_seconds": round(max(0.0, monotonic() - _PROCESS_STARTED_AT), 3),
    }


@router.get("/health", include_in_schema=False)
async def health() -> HealthPayload:
    """Process-level readiness; no external dependency is required."""
    return _release_info()


@router.get("/health/db", include_in_schema=False)
async def database_health(db: Db) -> HealthPayload:
    """Prove the configured database accepts a round trip."""
    started_at = monotonic()
    await db.execute(text("SELECT 1"))
    database_latency_ms = round((monotonic() - started_at) * 1000, 3)
    payload = _release_info()
    payload.update(database="ok", database_latency_ms=database_latency_ms)
    return payload

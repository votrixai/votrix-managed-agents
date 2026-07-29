from __future__ import annotations

from app.config import get_settings
from app.governance import GovernanceLimits, GovernanceService, QuotaDecision


def governance_service() -> GovernanceService:
    settings = get_settings()
    return GovernanceService(
        GovernanceLimits(
            requests_per_minute=settings.vma_requests_per_minute,
            max_active_work=settings.vma_max_active_work,
            daily_model_tokens=settings.vma_daily_model_tokens,
            storage_bytes=settings.vma_organization_storage_bytes,
        )
    )


def rate_limit_headers(decision: QuotaDecision) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
    }
    if decision.reset_at is not None:
        headers["X-RateLimit-Reset"] = str(
            max(0, int(decision.reset_at.timestamp()))
        )
    if decision.retry_after_seconds is not None:
        headers["Retry-After"] = str(decision.retry_after_seconds)
    return headers


def quota_headers(decision: QuotaDecision) -> dict[str, str]:
    headers = {
        "X-Quota-Metric": decision.metric,
        "X-Quota-Limit": str(decision.limit),
        "X-Quota-Remaining": str(decision.remaining),
    }
    if decision.reset_at is not None:
        headers["X-Quota-Reset"] = str(max(0, int(decision.reset_at.timestamp())))
    if decision.retry_after_seconds is not None:
        headers["Retry-After"] = str(decision.retry_after_seconds)
    return headers

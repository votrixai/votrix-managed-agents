from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import session_scope
from app.db.queries import governance as governance_q

UTC = timezone.utc
ACTIVE_GAUGE_WINDOW = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class GovernanceLimits:
    requests_per_minute: int = 120
    max_active_work: int = 5
    daily_model_tokens: int = 1_000_000
    storage_bytes: int = 5 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name, value in (
            ("requests_per_minute", self.requests_per_minute),
            ("max_active_work", self.max_active_work),
            ("daily_model_tokens", self.daily_model_tokens),
            ("storage_bytes", self.storage_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True)
class WorkspaceQuotaOverrides:
    requests_per_minute: int | None = None
    max_active_work: int | None = None
    daily_model_tokens: int | None = None
    storage_bytes: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("requests_per_minute", self.requests_per_minute),
            ("max_active_work", self.max_active_work),
            ("daily_model_tokens", self.daily_model_tokens),
            ("storage_bytes", self.storage_bytes),
        ):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    metric: str
    limit: int
    used: int
    remaining: int
    window_start: datetime | None = None
    reset_at: datetime | None = None
    retry_after_seconds: int | None = None
    reason: str | None = None
    idempotent: bool = False
    recorded: bool = False
    recorded_quantity: int | None = None
    over_limit_by: int = 0


@dataclass(frozen=True)
class WorkReleaseResult:
    released: bool
    active_work: int
    idempotent: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class GovernanceCleanupResult:
    request_counters_deleted: int
    idempotency_records_deleted: int


@dataclass(frozen=True)
class TenantIdempotencyClaim:
    disposition: Literal["acquired", "replay", "in_progress", "conflict"]
    record_id: str
    workspace_id: str
    operation: str
    request_fingerprint: str
    response_status: int | None = None
    response_body: dict[str, Any] | None = None

    @property
    def acquired(self) -> bool:
        return self.disposition == "acquired"

    @property
    def replay(self) -> bool:
        return self.disposition == "replay"

    @property
    def error_status(self) -> int | None:
        if self.disposition == "in_progress":
            return 409
        if self.disposition == "conflict":
            return 422
        return None


class QuotaExceededError(RuntimeError):
    def __init__(self, decision: QuotaDecision):
        self.decision = decision
        super().__init__(
            f"Workspace quota exceeded for {decision.metric}: "
            f"used={decision.used}, limit={decision.limit}"
        )


class UsageIdempotencyConflictError(RuntimeError):
    """A usage idempotency key was reused for a different metering fact."""


DEFAULT_GOVERNANCE_LIMITS = GovernanceLimits()


class GovernanceService:
    """Durable tenant governance with no dependency on HTTP or runtime code."""

    def __init__(self, defaults: GovernanceLimits = DEFAULT_GOVERNANCE_LIMITS):
        self.defaults = defaults

    async def effective_limits(self, workspace_id: str) -> GovernanceLimits:
        async with session_scope() as db:
            return await resolve_workspace_limits(db, workspace_id, defaults=self.defaults)

    async def configure_workspace(
        self,
        workspace_id: str,
        overrides: WorkspaceQuotaOverrides,
        *,
        actor_type: str = "system",
        actor_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GovernanceLimits:
        async with session_scope() as db:
            await governance_q.set_workspace_quota_overrides(
                db,
                workspace_id=workspace_id,
                requests_per_minute=overrides.requests_per_minute,
                max_active_work=overrides.max_active_work,
                daily_model_tokens=overrides.daily_model_tokens,
                storage_bytes=overrides.storage_bytes,
                metadata=metadata,
            )
            limits = _resolve_limits(overrides, self.defaults)
            await governance_q.append_audit_entry(
                db,
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action="workspace.quota.configure",
                outcome="success",
                resource_type="workspace",
                resource_id=workspace_id,
                request_id=request_id,
                data={
                    "overrides": _limits_dict(overrides),
                    "effective_limits": _limits_dict(limits),
                },
            )
            await db.commit()
            return limits

    async def authorize_request(
        self,
        workspace_id: str,
        *,
        actor_type: str = "api_key",
        actor_id: str | None = None,
        request_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        cost: int = 1,
        now: datetime | None = None,
        audit: bool = True,
    ) -> QuotaDecision:
        _positive_int(cost, "cost")
        instant = _as_utc(now)
        window_start = instant.replace(second=0, microsecond=0)
        reset_at = window_start + timedelta(minutes=1)
        async with session_scope() as db:
            limits = await resolve_workspace_limits(db, workspace_id, defaults=self.defaults)
            allowed, used = await governance_q.consume_counter(
                db,
                workspace_id=workspace_id,
                metric=governance_q.REQUESTS_METRIC,
                window_start=window_start,
                window_seconds=60,
                amount=cost,
                limit=limits.requests_per_minute,
            )
            decision = _counter_decision(
                allowed=allowed,
                metric=governance_q.REQUESTS_METRIC,
                limit=limits.requests_per_minute,
                used=used,
                window_start=window_start,
                reset_at=reset_at,
                now=instant,
            )
            if audit:
                await governance_q.append_audit_entry(
                    db,
                    workspace_id=workspace_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="api.request.authorize",
                    outcome="success" if allowed else "denied",
                    resource_type="http_endpoint",
                    resource_id=path,
                    request_id=request_id,
                    data={
                        "method": method,
                        "path": path,
                        "cost": cost,
                        "quota": _decision_data(decision),
                    },
                    occurred_at=instant,
                )
            await db.commit()
            return decision

    async def acquire_active_work(
        self,
        workspace_id: str,
        reference_id: str,
        *,
        actor_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> QuotaDecision:
        reference_id = _required_text(reference_id, "reference_id", max_length=255)
        instant = _as_utc(now)
        async with session_scope() as db:
            decision = await self.acquire_active_work_in_session(
                db,
                workspace_id=workspace_id,
                reference_id=reference_id,
                actor_id=actor_id,
                request_id=request_id,
                metadata=metadata,
                now=instant,
            )
            await db.commit()
            return decision

    async def acquire_active_work_in_session(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        reference_id: str,
        actor_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> QuotaDecision:
        reference_id = _required_text(reference_id, "reference_id", max_length=255)
        instant = _as_utc(now)
        limits = await resolve_workspace_limits(db, workspace_id, defaults=self.defaults)
        reservation, created = await governance_q.claim_quota_reservation(
            db,
            workspace_id=workspace_id,
            quota_name=governance_q.ACTIVE_WORK_METRIC,
            reference_id=reference_id,
            amount=1,
            acquired_at=instant,
            metadata=metadata,
        )
        if not created:
            used = await governance_q.get_counter_value(
                db,
                workspace_id=workspace_id,
                metric=governance_q.ACTIVE_WORK_METRIC,
                window_start=ACTIVE_GAUGE_WINDOW,
            )
            decision = _active_work_decision(
                allowed=reservation.state == "active",
                limit=limits.max_active_work,
                used=used,
                reason=None if reservation.state == "active" else "reservation_released",
                idempotent=True,
            )
        else:
            allowed, used = await governance_q.consume_counter(
                db,
                workspace_id=workspace_id,
                metric=governance_q.ACTIVE_WORK_METRIC,
                window_start=ACTIVE_GAUGE_WINDOW,
                window_seconds=0,
                amount=1,
                limit=limits.max_active_work,
            )
            decision = _active_work_decision(
                allowed=allowed,
                limit=limits.max_active_work,
                used=used,
                reason=None if allowed else "active_work_limit",
            )
            if not allowed:
                await governance_q.discard_quota_reservation(db, reservation)

        await governance_q.append_audit_entry(
            db,
            workspace_id=workspace_id,
            actor_type="worker",
            actor_id=actor_id,
            action="work.quota.acquire",
            outcome="success" if decision.allowed else "denied",
            resource_type="environment_work",
            resource_id=reference_id,
            request_id=request_id,
            data={"quota": _decision_data(decision), "metadata": metadata or {}},
            occurred_at=instant,
        )
        return decision

    async def release_active_work(
        self,
        workspace_id: str,
        reference_id: str,
        *,
        actor_id: str | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> WorkReleaseResult:
        reference_id = _required_text(reference_id, "reference_id", max_length=255)
        instant = _as_utc(now)
        async with session_scope() as db:
            result = await self.release_active_work_in_session(
                db,
                workspace_id=workspace_id,
                reference_id=reference_id,
                actor_id=actor_id,
                request_id=request_id,
                now=instant,
            )
            await db.commit()
            return result

    async def release_active_work_in_session(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        reference_id: str,
        actor_id: str | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> WorkReleaseResult:
        reference_id = _required_text(reference_id, "reference_id", max_length=255)
        instant = _as_utc(now)
        reservation, released = await governance_q.release_quota_reservation(
            db,
            workspace_id=workspace_id,
            quota_name=governance_q.ACTIVE_WORK_METRIC,
            reference_id=reference_id,
            released_at=instant,
        )
        if reservation is None:
            used = await governance_q.get_counter_value(
                db,
                workspace_id=workspace_id,
                metric=governance_q.ACTIVE_WORK_METRIC,
                window_start=ACTIVE_GAUGE_WINDOW,
            )
            result = WorkReleaseResult(False, used, reason="reservation_not_found")
        elif not released:
            used = await governance_q.get_counter_value(
                db,
                workspace_id=workspace_id,
                metric=governance_q.ACTIVE_WORK_METRIC,
                window_start=ACTIVE_GAUGE_WINDOW,
            )
            result = WorkReleaseResult(False, used, idempotent=True, reason="already_released")
        else:
            used = await governance_q.adjust_counter(
                db,
                workspace_id=workspace_id,
                metric=governance_q.ACTIVE_WORK_METRIC,
                window_start=ACTIVE_GAUGE_WINDOW,
                window_seconds=0,
                delta=-reservation.amount,
            )
            result = WorkReleaseResult(True, used)
        await governance_q.append_audit_entry(
            db,
            workspace_id=workspace_id,
            actor_type="worker",
            actor_id=actor_id,
            action="work.quota.release",
            outcome="success" if result.released or result.idempotent else "not_found",
            resource_type="environment_work",
            resource_id=reference_id,
            request_id=request_id,
            data={
                "active_work": result.active_work,
                "idempotent": result.idempotent,
                "reason": result.reason,
            },
            occurred_at=instant,
        )
        return result

    async def preflight_model_tokens(
        self,
        workspace_id: str,
        *,
        estimated_tokens: int = 0,
        actor_id: str | None = None,
        request_id: str | None = None,
        source_type: str | None = "session",
        source_id: str | None = None,
        now: datetime | None = None,
        audit: bool = True,
    ) -> QuotaDecision:
        instant = _as_utc(now)
        async with session_scope() as db:
            decision = await self.preflight_model_tokens_in_session(
                db,
                workspace_id=workspace_id,
                estimated_tokens=estimated_tokens,
                actor_id=actor_id,
                request_id=request_id,
                source_type=source_type,
                source_id=source_id,
                now=instant,
                audit=audit,
            )
            await db.commit()
            return decision

    async def preflight_model_tokens_in_session(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        estimated_tokens: int = 0,
        actor_id: str | None = None,
        request_id: str | None = None,
        source_type: str | None = "session",
        source_id: str | None = None,
        now: datetime | None = None,
        audit: bool = True,
    ) -> QuotaDecision:
        """Check a turn before provider execution without pretending usage is known.

        A zero estimate admits a turn while the workspace is below its daily
        limit. If that turn's actual usage crosses the limit, postflight still
        records all tokens and reports the overrun; subsequent preflights fail.
        """
        _nonnegative_int(estimated_tokens, "estimated_tokens")
        instant = _as_utc(now)
        limits = await resolve_workspace_limits(db, workspace_id, defaults=self.defaults)
        window_start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
        reset_at = window_start + timedelta(days=1)
        used = await governance_q.get_counter_value(
            db,
            workspace_id=workspace_id,
            metric=governance_q.MODEL_TOKENS_METRIC,
            window_start=window_start,
        )
        allowed = used < limits.daily_model_tokens and (
            estimated_tokens == 0 or used + estimated_tokens <= limits.daily_model_tokens
        )
        decision = QuotaDecision(
            allowed=allowed,
            metric=governance_q.MODEL_TOKENS_METRIC,
            limit=limits.daily_model_tokens,
            used=used,
            remaining=max(limits.daily_model_tokens - used, 0),
            window_start=window_start,
            reset_at=reset_at,
            retry_after_seconds=(
                None if allowed else max(1, math.ceil((reset_at - instant).total_seconds()))
            ),
            reason=None if allowed else "model_tokens_limit",
            over_limit_by=max(used - limits.daily_model_tokens, 0),
        )
        if audit:
            await governance_q.append_audit_entry(
                db,
                workspace_id=workspace_id,
                actor_type="runtime",
                actor_id=actor_id,
                action="model_tokens.preflight",
                outcome="success" if allowed else "denied",
                resource_type=source_type,
                resource_id=source_id,
                request_id=request_id,
                data={
                    "estimated_tokens": estimated_tokens,
                    "quota": _decision_data(decision),
                },
                occurred_at=instant,
            )
        return decision

    async def postflight_model_tokens(
        self,
        workspace_id: str,
        total_tokens: int,
        *,
        idempotency_key: str,
        provider: str | None = None,
        model: str | None = None,
        source_type: str | None = "session",
        source_id: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
        dimensions: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> QuotaDecision:
        instant = _as_utc(now)
        async with session_scope() as db:
            decision = await self.postflight_model_tokens_in_session(
                db,
                workspace_id=workspace_id,
                total_tokens=total_tokens,
                idempotency_key=idempotency_key,
                provider=provider,
                model=model,
                source_type=source_type,
                source_id=source_id,
                actor_id=actor_id,
                request_id=request_id,
                dimensions=dimensions,
                data=data,
                now=instant,
            )
            await db.commit()
            return decision

    async def postflight_model_tokens_in_session(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        total_tokens: int,
        idempotency_key: str,
        provider: str | None = None,
        model: str | None = None,
        source_type: str | None = "session",
        source_id: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
        dimensions: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> QuotaDecision:
        """Record actual provider usage exactly once, even when it exceeds quota."""
        _nonnegative_int(total_tokens, "total_tokens")
        normalized_key = _required_text(idempotency_key, "idempotency_key", max_length=255)
        instant = _as_utc(now)
        usage_data = dict(data or {})
        usage_data["accounting_phase"] = "postflight_actual"
        entry, created = await governance_q.append_usage_entry_once(
            db,
            workspace_id=workspace_id,
            metric=governance_q.MODEL_TOKENS_METRIC,
            quantity=total_tokens,
            unit="token",
            provider=provider,
            model=model,
            source_type=source_type,
            source_id=source_id,
            idempotency_key=normalized_key,
            dimensions=dimensions,
            data=usage_data,
            occurred_at=instant,
        )
        if not created:
            _require_matching_usage(
                entry,
                metric=governance_q.MODEL_TOKENS_METRIC,
                quantity=total_tokens,
                unit="token",
                provider=provider,
                model=model,
                source_type=source_type,
                source_id=source_id,
                dimensions=dimensions,
            )
            decision = await _model_token_idempotent_decision(
                db,
                workspace_id=workspace_id,
                entry=entry,
                defaults=self.defaults,
            )
        else:
            limits = await resolve_workspace_limits(db, workspace_id, defaults=self.defaults)
            window_start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
            used = await governance_q.adjust_counter(
                db,
                workspace_id=workspace_id,
                metric=governance_q.MODEL_TOKENS_METRIC,
                window_start=window_start,
                window_seconds=24 * 60 * 60,
                delta=total_tokens,
            )
            over_limit_by = max(used - limits.daily_model_tokens, 0)
            decision = QuotaDecision(
                allowed=over_limit_by == 0,
                metric=governance_q.MODEL_TOKENS_METRIC,
                limit=limits.daily_model_tokens,
                used=used,
                remaining=max(limits.daily_model_tokens - used, 0),
                window_start=window_start,
                reset_at=window_start + timedelta(days=1),
                reason="model_tokens_limit_overrun" if over_limit_by else None,
                recorded=True,
                recorded_quantity=total_tokens,
                over_limit_by=over_limit_by,
            )
        await governance_q.append_audit_entry(
            db,
            workspace_id=workspace_id,
            actor_type="runtime",
            actor_id=actor_id,
            action="model_tokens.postflight",
            outcome=(
                "replay" if decision.idempotent else "overrun" if decision.over_limit_by else "success"
            ),
            resource_type=source_type,
            resource_id=source_id,
            request_id=request_id,
            data={
                "provider": provider,
                "model": model,
                "total_tokens": total_tokens,
                "idempotency_key": normalized_key,
                "quota": _decision_data(decision),
            },
            occurred_at=instant,
        )
        return decision

    async def consume_model_tokens(
        self,
        workspace_id: str,
        total_tokens: int,
        *,
        idempotency_key: str,
        provider: str | None = None,
        model: str | None = None,
        source_type: str | None = "session",
        source_id: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
        dimensions: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> QuotaDecision:
        """Backward-compatible alias for postflight actual-usage accounting."""
        return await self.postflight_model_tokens(
            workspace_id,
            total_tokens,
            idempotency_key=idempotency_key,
            provider=provider,
            model=model,
            source_type=source_type,
            source_id=source_id,
            actor_id=actor_id,
            request_id=request_id,
            dimensions=dimensions,
            data=data,
            now=now,
        )

    async def record_audit(
        self,
        workspace_id: str,
        *,
        actor_type: str,
        actor_id: str | None,
        action: str,
        outcome: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ):
        async with session_scope() as db:
            entry = await governance_q.append_audit_entry(
                db,
                workspace_id=workspace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                outcome=outcome,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                data=data,
                occurred_at=_as_utc(occurred_at),
            )
            await db.commit()
            return entry

    async def record_usage(
        self,
        workspace_id: str,
        *,
        metric: str,
        quantity: int,
        unit: str,
        idempotency_key: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        dimensions: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ):
        _nonnegative_int(quantity, "quantity")
        instant = _as_utc(occurred_at)
        async with session_scope() as db:
            if idempotency_key:
                normalized_key = _required_text(
                    idempotency_key,
                    "idempotency_key",
                    max_length=255,
                )
                entry, created = await governance_q.append_usage_entry_once(
                    db,
                    workspace_id=workspace_id,
                    metric=metric,
                    quantity=quantity,
                    unit=unit,
                    provider=provider,
                    model=model,
                    source_type=source_type,
                    source_id=source_id,
                    idempotency_key=normalized_key,
                    dimensions=dimensions,
                    data=data,
                    occurred_at=instant,
                )
                if not created:
                    _require_matching_usage(
                        entry,
                        metric=metric,
                        quantity=quantity,
                        unit=unit,
                        provider=provider,
                        model=model,
                        source_type=source_type,
                        source_id=source_id,
                        dimensions=dimensions,
                    )
            else:
                entry = await governance_q.append_usage_entry(
                    db,
                    workspace_id=workspace_id,
                    metric=metric,
                    quantity=quantity,
                    unit=unit,
                    provider=provider,
                    model=model,
                    source_type=source_type,
                    source_id=source_id,
                    idempotency_key=None,
                    dimensions=dimensions,
                    data=data,
                    occurred_at=instant,
                )
            await db.commit()
            return entry

    async def enforce_storage_quota(
        self,
        db: AsyncSession,
        workspace_id: str,
        incoming_bytes: int,
    ) -> QuotaDecision:
        return await enforce_storage_quota(
            db,
            workspace_id,
            incoming_bytes,
            defaults=self.defaults,
        )

    async def claim_idempotency(
        self,
        db: AsyncSession,
        *,
        workspace_id: str,
        operation: str,
        idempotency_key: str,
        request_payload: Any,
    ) -> TenantIdempotencyClaim:
        return await claim_tenant_idempotency(
            db,
            workspace_id=workspace_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

    async def complete_idempotency(
        self,
        db: AsyncSession,
        claim: TenantIdempotencyClaim,
        *,
        response_status: int,
        response_body: dict[str, Any],
    ) -> None:
        await complete_tenant_idempotency(
            db,
            claim,
            response_status=response_status,
            response_body=response_body,
        )

    async def cleanup_retained_state(
        self,
        *,
        expired_request_counters_before: datetime,
        completed_idempotency_before: datetime,
        batch_size: int = 500,
    ) -> GovernanceCleanupResult:
        _bounded_batch_size(batch_size)
        async with session_scope() as db:
            result = await cleanup_retained_state_in_session(
                db,
                expired_request_counters_before=expired_request_counters_before,
                completed_idempotency_before=completed_idempotency_before,
                batch_size=batch_size,
            )
            await db.commit()
            return result


async def acquire_active_work_in_session(
    db: AsyncSession,
    *,
    workspace_id: str,
    reference_id: str,
    actor_id: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
    defaults: GovernanceLimits = DEFAULT_GOVERNANCE_LIMITS,
) -> QuotaDecision:
    """Public transaction-scoped active-work acquisition helper."""
    return await GovernanceService(defaults).acquire_active_work_in_session(
        db,
        workspace_id=workspace_id,
        reference_id=reference_id,
        actor_id=actor_id,
        request_id=request_id,
        metadata=metadata,
        now=now,
    )


async def release_active_work_in_session(
    db: AsyncSession,
    *,
    workspace_id: str,
    reference_id: str,
    actor_id: str | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
    defaults: GovernanceLimits = DEFAULT_GOVERNANCE_LIMITS,
) -> WorkReleaseResult:
    """Public transaction-scoped idempotent active-work release helper."""
    return await GovernanceService(defaults).release_active_work_in_session(
        db,
        workspace_id=workspace_id,
        reference_id=reference_id,
        actor_id=actor_id,
        request_id=request_id,
        now=now,
    )


async def cleanup_retained_state_in_session(
    db: AsyncSession,
    *,
    expired_request_counters_before: datetime,
    completed_idempotency_before: datetime,
    batch_size: int = 500,
) -> GovernanceCleanupResult:
    """Bounded cleanup for data with caller-selected retention cutoffs."""
    _bounded_batch_size(batch_size)
    request_count = await governance_q.cleanup_expired_request_counters(
        db,
        expired_before=_as_utc(expired_request_counters_before),
        limit=batch_size,
    )
    idempotency_count = await governance_q.cleanup_completed_tenant_idempotency(
        db,
        completed_before=_as_utc(completed_idempotency_before),
        limit=batch_size,
    )
    return GovernanceCleanupResult(
        request_counters_deleted=request_count,
        idempotency_records_deleted=idempotency_count,
    )


async def resolve_workspace_limits(
    db: AsyncSession,
    workspace_id: str,
    *,
    defaults: GovernanceLimits = DEFAULT_GOVERNANCE_LIMITS,
) -> GovernanceLimits:
    quota = await governance_q.get_workspace_quota(db, workspace_id)
    if quota is None:
        return defaults
    return GovernanceLimits(
        requests_per_minute=(
            quota.requests_per_minute
            if quota.requests_per_minute is not None
            else defaults.requests_per_minute
        ),
        max_active_work=(
            quota.max_active_work
            if quota.max_active_work is not None
            else defaults.max_active_work
        ),
        daily_model_tokens=(
            quota.daily_model_tokens
            if quota.daily_model_tokens is not None
            else defaults.daily_model_tokens
        ),
        storage_bytes=(
            quota.storage_bytes if quota.storage_bytes is not None else defaults.storage_bytes
        ),
    )


async def enforce_storage_quota(
    db: AsyncSession,
    workspace_id: str,
    incoming_bytes: int,
    *,
    defaults: GovernanceLimits = DEFAULT_GOVERNANCE_LIMITS,
) -> QuotaDecision:
    """Lock, measure, and enforce storage in the caller's resource transaction.

    The caller must insert the File/Skill resource and commit using this same
    ``db`` session. Releasing the transaction before the insert would also
    release the per-workspace serialization lock.
    """
    _nonnegative_int(incoming_bytes, "incoming_bytes")
    quota = await governance_q.ensure_and_lock_workspace_quota(db, workspace_id)
    limits = GovernanceLimits(
        requests_per_minute=(
            quota.requests_per_minute
            if quota.requests_per_minute is not None
            else defaults.requests_per_minute
        ),
        max_active_work=(
            quota.max_active_work
            if quota.max_active_work is not None
            else defaults.max_active_work
        ),
        daily_model_tokens=(
            quota.daily_model_tokens
            if quota.daily_model_tokens is not None
            else defaults.daily_model_tokens
        ),
        storage_bytes=(
            quota.storage_bytes if quota.storage_bytes is not None else defaults.storage_bytes
        ),
    )
    used = await governance_q.workspace_storage_bytes(db, workspace_id)
    projected = used + incoming_bytes
    allowed = projected <= limits.storage_bytes
    decision = QuotaDecision(
        allowed=allowed,
        metric=governance_q.STORAGE_BYTES_METRIC,
        limit=limits.storage_bytes,
        used=used,
        remaining=max(limits.storage_bytes - used, 0),
        reason=None if allowed else "storage_limit",
    )
    if not allowed:
        raise QuotaExceededError(decision)
    return decision


async def claim_tenant_idempotency(
    db: AsyncSession,
    *,
    workspace_id: str,
    operation: str,
    idempotency_key: str,
    request_payload: Any,
) -> TenantIdempotencyClaim:
    """Claim an idempotency key inside the caller's operation transaction.

    Dispositions are ``acquired``, ``replay``, ``in_progress``, and
    ``conflict``. The caller should only perform the side effect for an
    acquired claim, return the stored response for a replay, use HTTP 409 for
    in-progress, and HTTP 422 for a fingerprint conflict.
    """
    operation = _required_text(operation, "operation", max_length=128)
    normalized_key = _required_text(idempotency_key, "idempotency_key", max_length=255)
    key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
    request_fingerprint = fingerprint_idempotency_request(request_payload)
    record, created = await governance_q.claim_tenant_idempotency(
        db,
        workspace_id=workspace_id,
        operation=operation,
        key_hash=key_hash,
        request_fingerprint=request_fingerprint,
    )
    if created:
        disposition = "acquired"
    elif record.request_fingerprint != request_fingerprint:
        disposition = "conflict"
    elif record.state == "completed":
        disposition = "replay"
    else:
        disposition = "in_progress"
    return TenantIdempotencyClaim(
        disposition=disposition,
        record_id=record.id,
        workspace_id=workspace_id,
        operation=operation,
        request_fingerprint=request_fingerprint,
        response_status=record.response_status,
        response_body=dict(record.response_body) if record.response_body is not None else None,
    )


async def complete_tenant_idempotency(
    db: AsyncSession,
    claim: TenantIdempotencyClaim,
    *,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    if not claim.acquired:
        raise ValueError("Only an acquired idempotency claim can be completed")
    if not 100 <= response_status <= 599:
        raise ValueError("response_status must be a valid HTTP status")
    record = await governance_q.get_tenant_idempotency_record(
        db,
        workspace_id=claim.workspace_id,
        record_id=claim.record_id,
        for_update=True,
    )
    if record is None:
        raise RuntimeError("Idempotency claim no longer exists")
    if record.request_fingerprint != claim.request_fingerprint:
        raise RuntimeError("Idempotency request fingerprint changed")
    if record.state == "completed":
        if record.response_status == response_status and record.response_body == response_body:
            return
        raise RuntimeError("Idempotency claim was already completed with a different response")
    current, completed = await governance_q.complete_tenant_idempotency(
        db,
        record,
        response_status=response_status,
        response_body=response_body,
    )
    if not completed:
        if current.response_status == response_status and current.response_body == response_body:
            return
        raise RuntimeError("Idempotency claim was concurrently completed with a different response")


def fingerprint_idempotency_request(request_payload: Any) -> str:
    if isinstance(request_payload, bytes):
        payload = request_payload
    elif isinstance(request_payload, str):
        payload = request_payload.encode("utf-8")
    else:
        payload = json.dumps(
            request_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _model_token_idempotent_decision(
    db: AsyncSession,
    *,
    workspace_id: str,
    entry,
    defaults: GovernanceLimits,
) -> QuotaDecision:
    limits = await resolve_workspace_limits(db, workspace_id, defaults=defaults)
    occurred_at = _as_utc(entry.occurred_at)
    window_start = occurred_at.replace(hour=0, minute=0, second=0, microsecond=0)
    used = await governance_q.get_counter_value(
        db,
        workspace_id=workspace_id,
        metric=governance_q.MODEL_TOKENS_METRIC,
        window_start=window_start,
    )
    over_limit_by = max(used - limits.daily_model_tokens, 0)
    return QuotaDecision(
        allowed=over_limit_by == 0,
        metric=governance_q.MODEL_TOKENS_METRIC,
        limit=limits.daily_model_tokens,
        used=used,
        remaining=max(limits.daily_model_tokens - used, 0),
        window_start=window_start,
        reset_at=window_start + timedelta(days=1),
        reason="model_tokens_limit_overrun" if over_limit_by else None,
        idempotent=True,
        recorded=True,
        recorded_quantity=entry.quantity,
        over_limit_by=over_limit_by,
    )


def _require_matching_usage(
    entry,
    *,
    metric: str,
    quantity: int,
    unit: str,
    provider: str | None,
    model: str | None,
    source_type: str | None,
    source_id: str | None,
    dimensions: dict[str, Any] | None,
) -> None:
    expected = (
        metric,
        quantity,
        unit,
        provider,
        model,
        source_type,
        source_id,
        dict(dimensions or {}),
    )
    actual = (
        entry.metric,
        entry.quantity,
        entry.unit,
        entry.provider,
        entry.model,
        entry.source_type,
        entry.source_id,
        entry.dimensions,
    )
    if actual != expected:
        raise UsageIdempotencyConflictError(
            "Usage idempotency key was reused for a different metering fact"
        )


def _resolve_limits(
    overrides: WorkspaceQuotaOverrides,
    defaults: GovernanceLimits,
) -> GovernanceLimits:
    return GovernanceLimits(
        requests_per_minute=(
            overrides.requests_per_minute
            if overrides.requests_per_minute is not None
            else defaults.requests_per_minute
        ),
        max_active_work=(
            overrides.max_active_work
            if overrides.max_active_work is not None
            else defaults.max_active_work
        ),
        daily_model_tokens=(
            overrides.daily_model_tokens
            if overrides.daily_model_tokens is not None
            else defaults.daily_model_tokens
        ),
        storage_bytes=(
            overrides.storage_bytes
            if overrides.storage_bytes is not None
            else defaults.storage_bytes
        ),
    )


def _counter_decision(
    *,
    allowed: bool,
    metric: str,
    limit: int,
    used: int,
    window_start: datetime,
    reset_at: datetime,
    now: datetime,
) -> QuotaDecision:
    return QuotaDecision(
        allowed=allowed,
        metric=metric,
        limit=limit,
        used=used,
        remaining=max(limit - used, 0),
        window_start=window_start,
        reset_at=reset_at,
        retry_after_seconds=(
            None if allowed else max(1, math.ceil((reset_at - now).total_seconds()))
        ),
        reason=None if allowed else f"{metric}_limit",
    )


def _active_work_decision(
    *,
    allowed: bool,
    limit: int,
    used: int,
    reason: str | None = None,
    idempotent: bool = False,
) -> QuotaDecision:
    return QuotaDecision(
        allowed=allowed,
        metric=governance_q.ACTIVE_WORK_METRIC,
        limit=limit,
        used=used,
        remaining=max(limit - used, 0),
        reason=reason,
        idempotent=idempotent,
    )


def _decision_data(decision: QuotaDecision) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "metric": decision.metric,
        "limit": decision.limit,
        "used": decision.used,
        "remaining": decision.remaining,
        "window_start": decision.window_start.isoformat() if decision.window_start else None,
        "reset_at": decision.reset_at.isoformat() if decision.reset_at else None,
        "retry_after_seconds": decision.retry_after_seconds,
        "reason": decision.reason,
        "idempotent": decision.idempotent,
        "recorded": decision.recorded,
        "recorded_quantity": decision.recorded_quantity,
        "over_limit_by": decision.over_limit_by,
    }


def _limits_dict(value: GovernanceLimits | WorkspaceQuotaOverrides) -> dict[str, int | None]:
    return {
        "requests_per_minute": value.requests_per_minute,
        "max_active_work": value.max_active_work,
        "daily_model_tokens": value.daily_model_tokens,
        "storage_bytes": value.storage_bytes,
    }


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _nonnegative_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _positive_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _required_text(value: str, field: str, *, max_length: int) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return normalized


def _bounded_batch_size(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise ValueError("batch_size must be an integer between 1 and 10000")

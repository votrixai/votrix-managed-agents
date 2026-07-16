from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.db.engine import session_scope
from app.db.models import (
    AppendOnlyLedgerError,
    AuditLedgerEntry,
    TenantIdempotencyRecord,
    UsageLedgerEntry,
    OrganizationQuotaCounter,
    OrganizationQuotaReservation,
)
from app.db.queries import governance as governance_q
from app.db.queries import resources as resources_q
from app.governance import (
    GovernanceLimits,
    GovernanceService,
    QuotaExceededError,
    UsageIdempotencyConflictError,
    acquire_active_work_in_session,
    claim_tenant_idempotency,
    complete_tenant_idempotency,
    enforce_storage_quota,
    fingerprint_idempotency_request,
    release_active_work_in_session,
)

UTC = timezone.utc


async def test_request_rate_limit_is_atomic_per_tenant_and_window() -> None:
    service = GovernanceService(GovernanceLimits(requests_per_minute=2))
    first_window = datetime(2026, 7, 15, 12, 0, 20, tzinfo=UTC)

    first = await service.authorize_request("org_a", now=first_window, audit=False)
    second = await service.authorize_request("org_a", now=first_window, audit=False)
    denied = await service.authorize_request("org_a", now=first_window, audit=False)
    other_tenant = await service.authorize_request("org_b", now=first_window, audit=False)
    next_window = await service.authorize_request(
        "org_a",
        now=first_window + timedelta(minutes=1),
        audit=False,
    )

    assert (first.allowed, first.used, first.remaining) == (True, 1, 1)
    assert (second.allowed, second.used, second.remaining) == (True, 2, 0)
    assert (denied.allowed, denied.used, denied.remaining) == (False, 2, 0)
    assert denied.retry_after_seconds == 40
    assert (other_tenant.allowed, other_tenant.used) == (True, 1)
    assert (next_window.allowed, next_window.used, next_window.remaining) == (True, 1, 1)


async def test_concurrent_rate_limit_never_crosses_limit() -> None:
    service = GovernanceService(GovernanceLimits(requests_per_minute=5))
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    decisions = await asyncio.gather(
        *(service.authorize_request("org_test", now=now, audit=False) for _ in range(10))
    )

    assert sum(decision.allowed for decision in decisions) == 5
    assert max(decision.used for decision in decisions) == 5
    async with session_scope() as db:
        stored = await governance_q.get_counter_value(
            db,
            organization_id="org_test",
            metric=governance_q.REQUESTS_METRIC,
            window_start=now,
        )
    assert stored == 5


async def test_active_work_in_session_acquire_release_and_idempotency() -> None:
    defaults = GovernanceLimits(max_active_work=2)
    async with session_scope() as db:
        first = await acquire_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_1",
            defaults=defaults,
        )
        replay = await acquire_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_1",
            defaults=defaults,
        )
        second = await acquire_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_2",
            defaults=defaults,
        )
        denied = await acquire_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_3",
            defaults=defaults,
        )
        isolated = await acquire_active_work_in_session(
            db,
            organization_id="org_b",
            reference_id="work_1",
            defaults=defaults,
        )
        released = await release_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_1",
            defaults=defaults,
        )
        released_again = await release_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_1",
            defaults=defaults,
        )
        reacquire_released = await acquire_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="work_1",
            defaults=defaults,
        )
        missing = await release_active_work_in_session(
            db,
            organization_id="org_a",
            reference_id="does_not_exist",
            defaults=defaults,
        )
        await db.commit()

    assert (first.allowed, first.used, first.idempotent) == (True, 1, False)
    assert (replay.allowed, replay.used, replay.idempotent) == (True, 1, True)
    assert (second.allowed, second.used) == (True, 2)
    assert (denied.allowed, denied.used, denied.reason) == (False, 2, "active_work_limit")
    assert (isolated.allowed, isolated.used) == (True, 1)
    assert (released.released, released.active_work) == (True, 1)
    assert (released_again.released, released_again.idempotent) == (False, True)
    assert released_again.active_work == 1
    assert (reacquire_released.allowed, reacquire_released.idempotent) == (False, True)
    assert reacquire_released.reason == "reservation_released"
    assert (missing.released, missing.reason) == (False, "reservation_not_found")

    async with session_scope() as db:
        reservations = (
            await db.execute(
                select(OrganizationQuotaReservation).where(
                    OrganizationQuotaReservation.organization_id == "org_a"
                )
            )
        ).scalars().all()
    assert {(item.reference_id, item.state) for item in reservations} == {
        ("work_1", "released"),
        ("work_2", "active"),
    }


async def test_concurrent_active_work_same_reference_counts_once() -> None:
    service = GovernanceService(GovernanceLimits(max_active_work=1))
    first, second = await asyncio.gather(
        service.acquire_active_work("org_test", "same_work"),
        service.acquire_active_work("org_test", "same_work"),
    )

    assert first.allowed and second.allowed
    assert {first.idempotent, second.idempotent} == {False, True}
    assert first.used == second.used == 1

    released = await asyncio.gather(
        service.release_active_work("org_test", "same_work"),
        service.release_active_work("org_test", "same_work"),
    )
    assert sum(result.released for result in released) == 1
    assert sum(result.idempotent for result in released) == 1
    assert all(result.active_work == 0 for result in released)


async def test_concurrent_active_work_different_references_respects_limit() -> None:
    service = GovernanceService(GovernanceLimits(max_active_work=1))
    decisions = await asyncio.gather(
        service.acquire_active_work("org_test", "work_a"),
        service.acquire_active_work("org_test", "work_b"),
    )
    assert sum(decision.allowed for decision in decisions) == 1
    assert {decision.used for decision in decisions} == {1}


async def test_storage_quota_counts_inline_external_and_live_tenant_resources() -> None:
    defaults = GovernanceLimits(storage_bytes=10)
    async with session_scope() as db:
        await resources_q.create_resource(
            db,
            resource_type="file",
            organization_id="org_a",
            content=b"1234",
            size_bytes=1,
            name="inline",
        )
        allowed = await enforce_storage_quota(db, "org_a", 6, defaults=defaults)
        external = await resources_q.create_resource(
            db,
            resource_type="file",
            organization_id="org_a",
            size_bytes=6,
            storage_backend="s3",
            storage_key="files/external",
            name="external",
        )
        with pytest.raises(QuotaExceededError) as exc_info:
            await enforce_storage_quota(db, "org_a", 1, defaults=defaults)
        isolated = await enforce_storage_quota(db, "org_b", 10, defaults=defaults)
        external.deleted_at = datetime.now(UTC)
        after_delete = await enforce_storage_quota(db, "org_a", 6, defaults=defaults)
        await db.commit()

    assert (allowed.allowed, allowed.used, allowed.remaining) == (True, 4, 6)
    assert exc_info.value.decision.used == 10
    assert exc_info.value.decision.reason == "storage_limit"
    assert (isolated.allowed, isolated.used) == (True, 0)
    assert (after_delete.allowed, after_delete.used) == (True, 4)


async def test_model_token_preflight_postflight_records_one_turn_overrun() -> None:
    service = GovernanceService(GovernanceLimits(daily_model_tokens=10))
    now = datetime(2026, 7, 15, 8, 0, 0, tzinfo=UTC)
    turn_2_dimensions = {
        "input_tokens": 2,
        "output_tokens": 2,
        "cache_read_tokens": 1,
        "reasoning_tokens": 0,
    }

    initial = await service.preflight_model_tokens("org_test", now=now, audit=False)
    first = await service.postflight_model_tokens(
        "org_test",
        8,
        idempotency_key="turn_1",
        provider="openai",
        model="gpt-test",
        source_id="session_1",
        now=now,
    )
    estimated_denied = await service.preflight_model_tokens(
        "org_test", estimated_tokens=3, now=now, audit=False
    )
    unknown_actual_allowed = await service.preflight_model_tokens("org_test", now=now, audit=False)
    overrun = await service.postflight_model_tokens(
        "org_test",
        5,
        idempotency_key="turn_2",
        provider="openai",
        model="gpt-test",
        source_id="session_1",
        dimensions=turn_2_dimensions,
        now=now,
    )
    subsequent = await service.preflight_model_tokens("org_test", now=now, audit=False)
    replay = await service.postflight_model_tokens(
        "org_test",
        5,
        idempotency_key="turn_2",
        provider="openai",
        model="gpt-test",
        source_id="session_1",
        dimensions=turn_2_dimensions,
        now=now,
    )

    assert (initial.allowed, initial.used) == (True, 0)
    assert (first.allowed, first.recorded, first.recorded_quantity, first.used) == (
        True,
        True,
        8,
        8,
    )
    assert not estimated_denied.allowed
    assert unknown_actual_allowed.allowed
    assert (overrun.allowed, overrun.recorded, overrun.used, overrun.over_limit_by) == (
        False,
        True,
        13,
        3,
    )
    assert overrun.reason == "model_tokens_limit_overrun"
    assert (subsequent.allowed, subsequent.used, subsequent.over_limit_by) == (False, 13, 3)
    assert (replay.idempotent, replay.recorded, replay.used, replay.over_limit_by) == (
        True,
        True,
        13,
        3,
    )

    async with session_scope() as db:
        total, entries = (
            await db.execute(
                select(func.sum(UsageLedgerEntry.quantity), func.count())
                .select_from(UsageLedgerEntry)
                .where(UsageLedgerEntry.organization_id == "org_test")
            )
        ).one()
        stored_turn_2 = await governance_q.get_usage_by_idempotency_key(
            db,
            organization_id="org_test",
            idempotency_key="turn_2",
        )
    assert (total, entries) == (13, 2)
    assert stored_turn_2 is not None
    assert stored_turn_2.dimensions == turn_2_dimensions

    with pytest.raises(UsageIdempotencyConflictError):
        await service.postflight_model_tokens(
            "org_test",
            6,
            idempotency_key="turn_2",
            provider="openai",
            model="gpt-test",
            source_id="session_1",
            dimensions=turn_2_dimensions,
            now=now,
        )


async def test_model_token_windows_are_daily_and_tenant_isolated() -> None:
    service = GovernanceService(GovernanceLimits(daily_model_tokens=5))
    first_day = datetime(2026, 7, 15, 23, 59, tzinfo=UTC)
    second_day = first_day + timedelta(minutes=2)
    await service.postflight_model_tokens("org_a", 5, idempotency_key="a-1", now=first_day)

    assert not (await service.preflight_model_tokens("org_a", now=first_day, audit=False)).allowed
    assert (await service.preflight_model_tokens("org_a", now=second_day, audit=False)).allowed
    assert (await service.preflight_model_tokens("org_b", now=first_day, audit=False)).allowed


async def test_concurrent_model_token_postflight_records_usage_event_once() -> None:
    service = GovernanceService(GovernanceLimits(daily_model_tokens=100))
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    first, second = await asyncio.gather(
        service.postflight_model_tokens("org_test", 7, idempotency_key="provider-usage", now=now),
        service.postflight_model_tokens("org_test", 7, idempotency_key="provider-usage", now=now),
    )
    assert {first.idempotent, second.idempotent} == {False, True}
    assert first.used == second.used == 7
    async with session_scope() as db:
        usage_count = await db.scalar(select(func.count()).select_from(UsageLedgerEntry))
    assert usage_count == 1


async def test_usage_and_audit_are_tenant_scoped_idempotent_and_append_only() -> None:
    service = GovernanceService()
    audit = await service.record_audit(
        "org_a",
        actor_type="api_key",
        actor_id="key_1",
        action="agent.create",
        outcome="success",
    )
    first = await service.record_usage(
        "org_a",
        metric="sandbox_seconds",
        quantity=12,
        unit="second",
        idempotency_key="sandbox_1",
        source_type="sandbox",
        source_id="sbx_1",
    )
    replay = await service.record_usage(
        "org_a",
        metric="sandbox_seconds",
        quantity=12,
        unit="second",
        idempotency_key="sandbox_1",
        source_type="sandbox",
        source_id="sbx_1",
    )
    await service.record_usage(
        "org_b",
        metric="sandbox_seconds",
        quantity=99,
        unit="second",
        idempotency_key="sandbox_1",
        source_type="sandbox",
        source_id="sbx_1",
    )
    assert first.id == replay.id

    with pytest.raises(UsageIdempotencyConflictError):
        await service.record_usage(
            "org_a",
            metric="sandbox_seconds",
            quantity=13,
            unit="second",
            idempotency_key="sandbox_1",
            source_type="sandbox",
            source_id="sbx_1",
        )

    async with session_scope() as db:
        audit_rows = await governance_q.list_audit_entries(db, organization_id="org_a")
        usage_rows = await governance_q.list_usage_entries(db, organization_id="org_a")
    assert [entry.id for entry in audit_rows] == [audit.id]
    assert [(entry.id, entry.quantity) for entry in usage_rows] == [(first.id, 12)]

    async with session_scope() as db:
        stored_audit = await db.get(AuditLedgerEntry, audit.id)
        assert stored_audit is not None
        stored_audit.outcome = "mutated"
        with pytest.raises(AppendOnlyLedgerError):
            await db.flush()
        await db.rollback()

    async with session_scope() as db:
        stored_usage = await db.get(UsageLedgerEntry, first.id)
        assert stored_usage is not None
        await db.delete(stored_usage)
        with pytest.raises(AppendOnlyLedgerError):
            await db.flush()
        await db.rollback()


async def test_generic_tenant_idempotency_claim_replay_conflict_and_isolation() -> None:
    payload = {"agent_id": "agent_1", "input": {"b": 2, "a": 1}}
    assert fingerprint_idempotency_request(payload) == fingerprint_idempotency_request(
        {"input": {"a": 1, "b": 2}, "agent_id": "agent_1"}
    )

    async with session_scope() as db:
        acquired = await claim_tenant_idempotency(
            db,
            organization_id="org_a",
            operation="session.create",
            idempotency_key="customer-request-1",
            request_payload=payload,
        )
        assert acquired.acquired
        await complete_tenant_idempotency(
            db,
            acquired,
            response_status=201,
            response_body={"id": "session_1"},
        )
        # Completing the original owner identically is safe inside its transaction.
        await complete_tenant_idempotency(
            db,
            acquired,
            response_status=201,
            response_body={"id": "session_1"},
        )
        await db.commit()

    async with session_scope() as db:
        replay = await claim_tenant_idempotency(
            db,
            organization_id="org_a",
            operation="session.create",
            idempotency_key="customer-request-1",
            request_payload={"input": {"a": 1, "b": 2}, "agent_id": "agent_1"},
        )
        conflict = await claim_tenant_idempotency(
            db,
            organization_id="org_a",
            operation="session.create",
            idempotency_key="customer-request-1",
            request_payload={"agent_id": "different"},
        )
        other_operation = await claim_tenant_idempotency(
            db,
            organization_id="org_a",
            operation="agent.create",
            idempotency_key="customer-request-1",
            request_payload=payload,
        )
        other_tenant = await claim_tenant_idempotency(
            db,
            organization_id="org_b",
            operation="session.create",
            idempotency_key="customer-request-1",
            request_payload=payload,
        )
        in_progress_owner = await claim_tenant_idempotency(
            db,
            organization_id="org_a",
            operation="session.create",
            idempotency_key="customer-request-2",
            request_payload=payload,
        )
        await db.commit()

    async with session_scope() as db:
        in_progress = await claim_tenant_idempotency(
            db,
            organization_id="org_a",
            operation="session.create",
            idempotency_key="customer-request-2",
            request_payload=payload,
        )
        records = (await db.execute(select(TenantIdempotencyRecord))).scalars().all()

    assert replay.replay
    assert (replay.response_status, replay.response_body) == (201, {"id": "session_1"})
    assert (conflict.disposition, conflict.error_status) == ("conflict", 422)
    assert other_operation.acquired and other_tenant.acquired and in_progress_owner.acquired
    assert (in_progress.disposition, in_progress.error_status) == ("in_progress", 409)
    assert all("customer-request" not in record.key_hash for record in records)
    assert all(len(record.key_hash) == 64 for record in records)


async def test_concurrent_generic_idempotency_claim_has_one_owner() -> None:
    async def claim_once():
        async with session_scope() as db:
            claim = await claim_tenant_idempotency(
                db,
                organization_id="org_test",
                operation="file.create",
                idempotency_key="one-owner",
                request_payload={"filename": "report.txt"},
            )
            await db.commit()
            return claim

    first, second = await asyncio.gather(claim_once(), claim_once())
    assert {first.disposition, second.disposition} == {"acquired", "in_progress"}
    assert first.record_id == second.record_id


async def test_bounded_cleanup_removes_only_expired_request_and_completed_records() -> None:
    service = GovernanceService(GovernanceLimits(requests_per_minute=10))
    now = datetime.now(UTC).replace(microsecond=0)
    old = now - timedelta(days=10)
    await service.authorize_request("org_test", now=old, audit=False)
    await service.authorize_request("org_test", now=old + timedelta(minutes=1), audit=False)
    await service.authorize_request("org_test", now=now, audit=False)

    completed_ids: list[str] = []
    async with session_scope() as db:
        for number in range(2):
            claim = await claim_tenant_idempotency(
                db,
                organization_id="org_test",
                operation="cleanup.test",
                idempotency_key=f"completed-{number}",
                request_payload={"number": number},
            )
            await complete_tenant_idempotency(
                db,
                claim,
                response_status=200,
                response_body={"ok": True},
            )
            completed_ids.append(claim.record_id)
        active = await claim_tenant_idempotency(
            db,
            organization_id="org_test",
            operation="cleanup.test",
            idempotency_key="still-in-progress",
            request_payload={},
        )
        await db.flush()
        await db.execute(
            update(TenantIdempotencyRecord)
            .where(
                TenantIdempotencyRecord.id.in_([*completed_ids, active.record_id])
            )
            .values(updated_at=old)
        )
        # An old non-request metric must never be swept by request retention.
        await governance_q.adjust_counter(
            db,
            organization_id="org_test",
            metric=governance_q.MODEL_TOKENS_METRIC,
            window_start=old.replace(hour=0, minute=0, second=0),
            window_seconds=86_400,
            delta=1,
        )
        await db.commit()

    cutoff = now - timedelta(days=1)
    first_batch = await service.cleanup_retained_state(
        expired_request_counters_before=cutoff,
        completed_idempotency_before=cutoff,
        batch_size=1,
    )
    second_batch = await service.cleanup_retained_state(
        expired_request_counters_before=cutoff,
        completed_idempotency_before=cutoff,
        batch_size=1,
    )
    final_batch = await service.cleanup_retained_state(
        expired_request_counters_before=cutoff,
        completed_idempotency_before=cutoff,
        batch_size=1,
    )

    assert first_batch.request_counters_deleted == first_batch.idempotency_records_deleted == 1
    assert second_batch.request_counters_deleted == second_batch.idempotency_records_deleted == 1
    assert final_batch.request_counters_deleted == final_batch.idempotency_records_deleted == 0
    async with session_scope() as db:
        request_counters = await db.scalar(
            select(func.count())
            .select_from(OrganizationQuotaCounter)
            .where(OrganizationQuotaCounter.metric == governance_q.REQUESTS_METRIC)
        )
        model_counters = await db.scalar(
            select(func.count())
            .select_from(OrganizationQuotaCounter)
            .where(OrganizationQuotaCounter.metric == governance_q.MODEL_TOKENS_METRIC)
        )
        remaining_idempotency = (
            await db.execute(select(TenantIdempotencyRecord))
        ).scalars().all()
    assert request_counters == 1
    assert model_counters == 1
    assert [(record.id, record.state) for record in remaining_idempotency] == [
        (active.record_id, "in_progress")
    ]

    with pytest.raises(ValueError):
        await service.cleanup_retained_state(
            expired_request_counters_before=cutoff,
            completed_idempotency_before=cutoff,
            batch_size=0,
        )

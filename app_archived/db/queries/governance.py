from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLedgerEntry,
    ManagedResource,
    TenantIdempotencyRecord,
    UsageLedgerEntry,
    OrganizationQuota,
    OrganizationQuotaCounter,
    OrganizationQuotaReservation,
)
from app.ids import new_id

REQUESTS_METRIC = "requests"
ACTIVE_WORK_METRIC = "active_work"
MODEL_TOKENS_METRIC = "model_tokens"
STORAGE_BYTES_METRIC = "storage_bytes"
REQUEST_WINDOW_SECONDS = 60
USAGE_PAGE_CURSOR_VERSION = 1


class UsagePageCursorError(ValueError):
    """Raised when an opaque usage cursor is invalid for its query."""


@dataclass(frozen=True)
class UsageEntriesPage:
    entries: list[UsageLedgerEntry]
    next_page: str | None


async def get_organization_quota(
    db: AsyncSession,
    organization_id: str,
    *,
    for_update: bool = False,
) -> OrganizationQuota | None:
    stmt = select(OrganizationQuota).where(OrganizationQuota.organization_id == organization_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def ensure_and_lock_organization_quota(
    db: AsyncSession,
    organization_id: str,
) -> OrganizationQuota:
    """Create and lock the serialization row used by storage quota checks."""
    now = _utcnow()
    insert = _dialect_insert(db, OrganizationQuota).values(
        organization_id=organization_id,
        requests_per_minute=None,
        max_active_work=None,
        daily_model_tokens=None,
        storage_bytes=None,
        metadata_={},
        created_at=now,
        updated_at=now,
    )
    insert = insert.on_conflict_do_nothing(index_elements=[OrganizationQuota.organization_id])
    await db.execute(insert)

    # PostgreSQL obtains a row lock here. SQLite ignores FOR UPDATE, so the
    # no-op UPDATE below deliberately acquires its database writer lock until
    # the caller commits the resource insert in the same transaction.
    quota = await get_organization_quota(db, organization_id, for_update=True)
    if quota is None:
        raise RuntimeError("Failed to initialize organization quota row")
    await db.execute(
        update(OrganizationQuota)
        .where(OrganizationQuota.organization_id == organization_id)
        .values(updated_at=OrganizationQuota.updated_at)
    )
    return quota


async def set_organization_quota_overrides(
    db: AsyncSession,
    *,
    organization_id: str,
    requests_per_minute: int | None,
    max_active_work: int | None,
    daily_model_tokens: int | None,
    storage_bytes: int | None,
    metadata: dict[str, Any] | None = None,
) -> OrganizationQuota:
    quota = await ensure_and_lock_organization_quota(db, organization_id)
    quota.requests_per_minute = requests_per_minute
    quota.max_active_work = max_active_work
    quota.daily_model_tokens = daily_model_tokens
    quota.storage_bytes = storage_bytes
    if metadata is not None:
        quota.metadata_ = dict(metadata)
    await db.flush()
    return quota


async def consume_counter(
    db: AsyncSession,
    *,
    organization_id: str,
    metric: str,
    window_start: datetime,
    window_seconds: int,
    amount: int,
    limit: int | None,
) -> tuple[bool, int]:
    """Atomically add to a quota counter without crossing ``limit``."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if window_seconds < 0:
        raise ValueError("window_seconds must be non-negative")
    if amount == 0:
        return True, await get_counter_value(
            db,
            organization_id=organization_id,
            metric=metric,
            window_start=window_start,
        )
    if limit is not None and amount > limit:
        return False, await get_counter_value(
            db,
            organization_id=organization_id,
            metric=metric,
            window_start=window_start,
        )

    now = _utcnow()
    insert = _dialect_insert(db, OrganizationQuotaCounter).values(
        organization_id=organization_id,
        metric=metric,
        window_start=window_start,
        window_seconds=window_seconds,
        value=amount,
        created_at=now,
        updated_at=now,
    )
    proposed_value = OrganizationQuotaCounter.value + amount
    kwargs: dict[str, Any] = {}
    if limit is not None:
        kwargs["where"] = proposed_value <= limit
    statement = insert.on_conflict_do_update(
        index_elements=[
            OrganizationQuotaCounter.organization_id,
            OrganizationQuotaCounter.metric,
            OrganizationQuotaCounter.window_start,
        ],
        set_={
            "value": proposed_value,
            "window_seconds": window_seconds,
            "updated_at": now,
        },
        **kwargs,
    ).returning(OrganizationQuotaCounter.value)
    result = await db.execute(statement)
    value = result.scalar_one_or_none()
    if value is not None:
        return True, int(value)
    return False, await get_counter_value(
        db,
        organization_id=organization_id,
        metric=metric,
        window_start=window_start,
    )


async def adjust_counter(
    db: AsyncSession,
    *,
    organization_id: str,
    metric: str,
    window_start: datetime,
    window_seconds: int,
    delta: int,
) -> int:
    """Atomically adjust a gauge, flooring releases at zero."""
    if window_seconds < 0:
        raise ValueError("window_seconds must be non-negative")
    now = _utcnow()
    initial_value = max(delta, 0)
    insert = _dialect_insert(db, OrganizationQuotaCounter).values(
        organization_id=organization_id,
        metric=metric,
        window_start=window_start,
        window_seconds=window_seconds,
        value=initial_value,
        created_at=now,
        updated_at=now,
    )
    proposed_value = OrganizationQuotaCounter.value + delta
    next_value = case((proposed_value < 0, 0), else_=proposed_value)
    statement = insert.on_conflict_do_update(
        index_elements=[
            OrganizationQuotaCounter.organization_id,
            OrganizationQuotaCounter.metric,
            OrganizationQuotaCounter.window_start,
        ],
        set_={
            "value": next_value,
            "window_seconds": window_seconds,
            "updated_at": now,
        },
    ).returning(OrganizationQuotaCounter.value)
    result = await db.execute(statement)
    return int(result.scalar_one())


async def get_counter_value(
    db: AsyncSession,
    *,
    organization_id: str,
    metric: str,
    window_start: datetime,
) -> int:
    result = await db.execute(
        select(OrganizationQuotaCounter.value).where(
            OrganizationQuotaCounter.organization_id == organization_id,
            OrganizationQuotaCounter.metric == metric,
            OrganizationQuotaCounter.window_start == window_start,
        )
    )
    value = result.scalar_one_or_none()
    return int(value or 0)


async def organization_storage_bytes(db: AsyncSession, organization_id: str) -> int:
    content_length = (
        func.length(ManagedResource.content)
        if _dialect_name(db) == "sqlite"
        else func.octet_length(ManagedResource.content)
    )
    # Trust neither metadata nor inline content alone when both exist. Taking
    # the larger value prevents a stale/incorrect size_bytes from undercounting
    # storage while remaining portable (SQLite has no GREATEST function).
    stored_size = case(
        (ManagedResource.size_bytes.is_(None), func.coalesce(content_length, 0)),
        (content_length.is_(None), ManagedResource.size_bytes),
        (ManagedResource.size_bytes >= content_length, ManagedResource.size_bytes),
        else_=content_length,
    )
    result = await db.execute(
        select(func.coalesce(func.sum(stored_size), 0)).where(
            ManagedResource.organization_id == organization_id,
            ManagedResource.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one())


async def get_quota_reservation(
    db: AsyncSession,
    *,
    organization_id: str,
    quota_name: str,
    reference_id: str,
    for_update: bool = False,
) -> OrganizationQuotaReservation | None:
    stmt = select(OrganizationQuotaReservation).where(
        OrganizationQuotaReservation.organization_id == organization_id,
        OrganizationQuotaReservation.quota_name == quota_name,
        OrganizationQuotaReservation.reference_id == reference_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def claim_quota_reservation(
    db: AsyncSession,
    *,
    organization_id: str,
    quota_name: str,
    reference_id: str,
    amount: int,
    acquired_at: datetime,
    metadata: dict[str, Any] | None = None,
) -> tuple[OrganizationQuotaReservation, bool]:
    """Atomically create a reservation or return its durable predecessor."""
    reservation_id = new_id("qres")
    now = _utcnow()
    insert = _dialect_insert(db, OrganizationQuotaReservation).values(
        id=reservation_id,
        organization_id=organization_id,
        quota_name=quota_name,
        reference_id=reference_id,
        amount=amount,
        state="active",
        acquired_at=acquired_at,
        released_at=None,
        metadata_=dict(metadata or {}),
        created_at=now,
        updated_at=now,
    )
    statement = insert.on_conflict_do_nothing(
        index_elements=[
            OrganizationQuotaReservation.organization_id,
            OrganizationQuotaReservation.quota_name,
            OrganizationQuotaReservation.reference_id,
        ]
    ).returning(OrganizationQuotaReservation.id)
    result = await db.execute(statement)
    inserted_id = result.scalar_one_or_none()
    if inserted_id is not None:
        reservation = await db.get(OrganizationQuotaReservation, inserted_id)
        if reservation is None:
            raise RuntimeError("Quota reservation disappeared after insert")
        return reservation, True

    reservation = await get_quota_reservation(
        db,
        organization_id=organization_id,
        quota_name=quota_name,
        reference_id=reference_id,
    )
    if reservation is None:
        raise RuntimeError("Quota reservation conflict did not resolve to a durable row")
    return reservation, False


async def discard_quota_reservation(
    db: AsyncSession,
    reservation: OrganizationQuotaReservation,
) -> None:
    """Discard a reservation created in the current, still-uncommitted transaction."""
    await db.execute(
        delete(OrganizationQuotaReservation).where(
            OrganizationQuotaReservation.id == reservation.id,
            OrganizationQuotaReservation.state == "active",
        )
    )


async def release_quota_reservation(
    db: AsyncSession,
    *,
    organization_id: str,
    quota_name: str,
    reference_id: str,
    released_at: datetime,
) -> tuple[OrganizationQuotaReservation | None, bool]:
    """Conditionally release once, with atomic semantics on SQLite/PostgreSQL."""
    statement = (
        update(OrganizationQuotaReservation)
        .where(
            OrganizationQuotaReservation.organization_id == organization_id,
            OrganizationQuotaReservation.quota_name == quota_name,
            OrganizationQuotaReservation.reference_id == reference_id,
            OrganizationQuotaReservation.state == "active",
        )
        .values(state="released", released_at=released_at, updated_at=_utcnow())
        .returning(OrganizationQuotaReservation.id)
    )
    result = await db.execute(statement)
    released_id = result.scalar_one_or_none()
    reservation = await get_quota_reservation(
        db,
        organization_id=organization_id,
        quota_name=quota_name,
        reference_id=reference_id,
    )
    return reservation, released_id is not None


async def append_audit_entry(
    db: AsyncSession,
    *,
    organization_id: str,
    actor_type: str,
    actor_id: str | None,
    action: str,
    outcome: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> AuditLedgerEntry:
    entry = AuditLedgerEntry(
        id=new_id("audit"),
        organization_id=organization_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        data=dict(data or {}),
        occurred_at=occurred_at or _utcnow(),
    )
    db.add(entry)
    await db.flush()
    return entry


async def append_usage_entry(
    db: AsyncSession,
    *,
    organization_id: str,
    metric: str,
    quantity: int,
    unit: str,
    provider: str | None = None,
    model: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    idempotency_key: str | None = None,
    dimensions: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> UsageLedgerEntry:
    entry = UsageLedgerEntry(
        id=new_id("usage"),
        organization_id=organization_id,
        metric=metric,
        quantity=quantity,
        unit=unit,
        provider=provider,
        model=model,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
        dimensions=dict(dimensions or {}),
        data=dict(data or {}),
        occurred_at=occurred_at or _utcnow(),
    )
    db.add(entry)
    await db.flush()
    return entry


async def append_usage_entry_once(
    db: AsyncSession,
    *,
    organization_id: str,
    metric: str,
    quantity: int,
    unit: str,
    idempotency_key: str,
    provider: str | None = None,
    model: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    dimensions: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> tuple[UsageLedgerEntry, bool]:
    """Append one usage fact for a tenant key without exception-driven races."""
    entry_id = new_id("usage")
    insert = _dialect_insert(db, UsageLedgerEntry).values(
        id=entry_id,
        organization_id=organization_id,
        metric=metric,
        quantity=quantity,
        unit=unit,
        provider=provider,
        model=model,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
        dimensions=dict(dimensions or {}),
        data=dict(data or {}),
        occurred_at=occurred_at or _utcnow(),
    )
    statement = insert.on_conflict_do_nothing(
        index_elements=[UsageLedgerEntry.organization_id, UsageLedgerEntry.idempotency_key]
    ).returning(UsageLedgerEntry.id)
    result = await db.execute(statement)
    inserted_id = result.scalar_one_or_none()
    if inserted_id is not None:
        entry = await db.get(UsageLedgerEntry, inserted_id)
        if entry is None:
            raise RuntimeError("Usage ledger entry disappeared after insert")
        return entry, True
    entry = await get_usage_by_idempotency_key(
        db,
        organization_id=organization_id,
        idempotency_key=idempotency_key,
    )
    if entry is None:
        raise RuntimeError("Usage idempotency conflict did not resolve to a durable row")
    return entry, False


async def get_usage_by_idempotency_key(
    db: AsyncSession,
    *,
    organization_id: str,
    idempotency_key: str,
) -> UsageLedgerEntry | None:
    result = await db.execute(
        select(UsageLedgerEntry).where(
            UsageLedgerEntry.organization_id == organization_id,
            UsageLedgerEntry.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def list_audit_entries(
    db: AsyncSession,
    *,
    organization_id: str,
    limit: int = 100,
) -> list[AuditLedgerEntry]:
    result = await db.execute(
        select(AuditLedgerEntry)
        .where(AuditLedgerEntry.organization_id == organization_id)
        .order_by(AuditLedgerEntry.occurred_at.desc(), AuditLedgerEntry.id.desc())
        .limit(max(1, min(limit, 1000)))
    )
    return list(result.scalars().all())


async def list_usage_entries(
    db: AsyncSession,
    *,
    organization_id: str,
    limit: int = 100,
    source_type: str | None = None,
    source_id: str | None = None,
    metric: str | None = None,
    occurred_at_gt: datetime | None = None,
    occurred_at_gte: datetime | None = None,
    occurred_at_lt: datetime | None = None,
    occurred_at_lte: datetime | None = None,
) -> list[UsageLedgerEntry]:
    page = await list_usage_entries_page(
        db,
        organization_id=organization_id,
        limit=limit,
        source_type=source_type,
        source_id=source_id,
        metric=metric,
        occurred_at_gt=occurred_at_gt,
        occurred_at_gte=occurred_at_gte,
        occurred_at_lt=occurred_at_lt,
        occurred_at_lte=occurred_at_lte,
    )
    return page.entries


async def list_usage_entries_page(
    db: AsyncSession,
    *,
    organization_id: str,
    limit: int = 100,
    page: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    metric: str | None = None,
    occurred_at_gt: datetime | None = None,
    occurred_at_gte: datetime | None = None,
    occurred_at_lt: datetime | None = None,
    occurred_at_lte: datetime | None = None,
) -> UsageEntriesPage:
    """Read one stable keyset page directly from the append-only ledger."""

    page_size = max(1, min(limit, 1000))
    normalized_filters = {
        "organization_id": str(organization_id),
        "source_type": _optional_text(source_type),
        "source_id": _optional_text(source_id),
        "metric": _optional_text(metric),
        "occurred_at_gt": _canonical_datetime(occurred_at_gt),
        "occurred_at_gte": _canonical_datetime(occurred_at_gte),
        "occurred_at_lt": _canonical_datetime(occurred_at_lt),
        "occurred_at_lte": _canonical_datetime(occurred_at_lte),
    }
    filter_fingerprint = hashlib.sha256(
        json.dumps(normalized_filters, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()

    statement = select(UsageLedgerEntry).where(
        UsageLedgerEntry.organization_id == organization_id
    )
    if normalized_filters["source_type"] is not None:
        statement = statement.where(
            UsageLedgerEntry.source_type == normalized_filters["source_type"]
        )
    if normalized_filters["source_id"] is not None:
        statement = statement.where(
            UsageLedgerEntry.source_id == normalized_filters["source_id"]
        )
    if normalized_filters["metric"] is not None:
        statement = statement.where(
            UsageLedgerEntry.metric == normalized_filters["metric"]
        )

    if occurred_at_gt is not None:
        statement = statement.where(UsageLedgerEntry.occurred_at > occurred_at_gt)
    if occurred_at_gte is not None:
        statement = statement.where(UsageLedgerEntry.occurred_at >= occurred_at_gte)
    if occurred_at_lt is not None:
        statement = statement.where(UsageLedgerEntry.occurred_at < occurred_at_lt)
    if occurred_at_lte is not None:
        statement = statement.where(UsageLedgerEntry.occurred_at <= occurred_at_lte)

    if page is not None:
        cursor_time, cursor_id = _decode_usage_page_cursor(
            page,
            filter_fingerprint=filter_fingerprint,
        )
        statement = statement.where(
            or_(
                UsageLedgerEntry.occurred_at < cursor_time,
                and_(
                    UsageLedgerEntry.occurred_at == cursor_time,
                    UsageLedgerEntry.id < cursor_id,
                ),
            )
        )

    result = await db.execute(
        statement.order_by(
            UsageLedgerEntry.occurred_at.desc(),
            UsageLedgerEntry.id.desc(),
        ).limit(page_size + 1)
    )
    rows = list(result.scalars().all())
    has_more = len(rows) > page_size
    entries = rows[:page_size]
    next_page = (
        _encode_usage_page_cursor(
            entries[-1],
            filter_fingerprint=filter_fingerprint,
        )
        if has_more and entries
        else None
    )
    return UsageEntriesPage(entries=entries, next_page=next_page)


def _encode_usage_page_cursor(
    entry: UsageLedgerEntry,
    *,
    filter_fingerprint: str,
) -> str:
    payload = json.dumps(
        {
            "v": USAGE_PAGE_CURSOR_VERSION,
            "occurred_at": _canonical_datetime(entry.occurred_at),
            "id": entry.id,
            "filters": filter_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "usage_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_usage_page_cursor(
    value: str,
    *,
    filter_fingerprint: str,
) -> tuple[datetime, str]:
    if not value.startswith("usage_"):
        raise UsagePageCursorError("Invalid usage page cursor")
    try:
        encoded = value.removeprefix("usage_")
        payload = json.loads(
            base64.urlsafe_b64decode(encoded.encode("ascii") + b"===").decode(
                "utf-8"
            )
        )
        version = int(payload["v"])
        occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
        entry_id = str(payload["id"])
        cursor_filters = str(payload["filters"])
    except Exception as exc:
        raise UsagePageCursorError("Invalid usage page cursor") from exc
    if (
        version != USAGE_PAGE_CURSOR_VERSION
        or not entry_id
        or cursor_filters != filter_fingerprint
    ):
        raise UsagePageCursorError("Invalid usage page cursor")
    return _as_utc_datetime(occurred_at), entry_id


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc_datetime(value).isoformat()


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


async def claim_tenant_idempotency(
    db: AsyncSession,
    *,
    organization_id: str,
    operation: str,
    key_hash: str,
    request_fingerprint: str,
) -> tuple[TenantIdempotencyRecord, bool]:
    """Atomically reserve a tenant/operation/key tuple.

    Returns ``(record, created)``. A false ``created`` result must be inspected
    for fingerprint conflicts, an in-progress owner, or a completed replay.
    """
    record_id = new_id("idem")
    now = _utcnow()
    insert = _dialect_insert(db, TenantIdempotencyRecord).values(
        id=record_id,
        organization_id=organization_id,
        operation=operation,
        key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        state="in_progress",
        response_status=None,
        response_body=None,
        created_at=now,
        updated_at=now,
    )
    statement = insert.on_conflict_do_nothing(
        index_elements=[
            TenantIdempotencyRecord.organization_id,
            TenantIdempotencyRecord.operation,
            TenantIdempotencyRecord.key_hash,
        ]
    ).returning(TenantIdempotencyRecord.id)
    result = await db.execute(statement)
    inserted_id = result.scalar_one_or_none()
    if inserted_id is not None:
        record = await db.get(TenantIdempotencyRecord, inserted_id)
        if record is None:
            raise RuntimeError("Idempotency reservation disappeared after insert")
        return record, True

    result = await db.execute(
        select(TenantIdempotencyRecord).where(
            TenantIdempotencyRecord.organization_id == organization_id,
            TenantIdempotencyRecord.operation == operation,
            TenantIdempotencyRecord.key_hash == key_hash,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise RuntimeError("Idempotency conflict did not resolve to a durable record")
    return record, False


async def get_tenant_idempotency_record(
    db: AsyncSession,
    *,
    organization_id: str,
    record_id: str,
    for_update: bool = False,
) -> TenantIdempotencyRecord | None:
    stmt = select(TenantIdempotencyRecord).where(
        TenantIdempotencyRecord.organization_id == organization_id,
        TenantIdempotencyRecord.id == record_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    stmt = stmt.execution_options(populate_existing=True)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def complete_tenant_idempotency(
    db: AsyncSession,
    record: TenantIdempotencyRecord,
    *,
    response_status: int,
    response_body: dict[str, Any],
) -> tuple[TenantIdempotencyRecord, bool]:
    statement = (
        update(TenantIdempotencyRecord)
        .where(
            TenantIdempotencyRecord.id == record.id,
            TenantIdempotencyRecord.organization_id == record.organization_id,
            TenantIdempotencyRecord.request_fingerprint == record.request_fingerprint,
            TenantIdempotencyRecord.state == "in_progress",
        )
        .values(
            state="completed",
            response_status=response_status,
            response_body=dict(response_body),
            updated_at=_utcnow(),
        )
        .returning(TenantIdempotencyRecord.id)
    )
    result = await db.execute(statement)
    completed_id = result.scalar_one_or_none()
    current = await get_tenant_idempotency_record(
        db,
        organization_id=record.organization_id,
        record_id=record.id,
    )
    if current is None:
        raise RuntimeError("Idempotency record disappeared during completion")
    return current, completed_id is not None


async def cleanup_expired_request_counters(
    db: AsyncSession,
    *,
    expired_before: datetime,
    limit: int,
) -> int:
    """Delete at most ``limit`` request windows that expired before a cutoff."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    window_cutoff = expired_before - timedelta(seconds=REQUEST_WINDOW_SECONDS)
    rows = list(
        (
            await db.execute(
                select(
                    OrganizationQuotaCounter.organization_id,
                    OrganizationQuotaCounter.window_start,
                )
                .where(
                    OrganizationQuotaCounter.metric == REQUESTS_METRIC,
                    OrganizationQuotaCounter.window_start <= window_cutoff,
                )
                .order_by(OrganizationQuotaCounter.window_start.asc())
                .limit(limit)
            )
        ).all()
    )
    for organization_id, window_start in rows:
        await db.execute(
            delete(OrganizationQuotaCounter).where(
                OrganizationQuotaCounter.organization_id == organization_id,
                OrganizationQuotaCounter.metric == REQUESTS_METRIC,
                OrganizationQuotaCounter.window_start == window_start,
            )
        )
    return len(rows)


async def cleanup_completed_tenant_idempotency(
    db: AsyncSession,
    *,
    completed_before: datetime,
    limit: int,
) -> int:
    """Delete at most ``limit`` completed records; in-progress claims survive."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    record_ids = list(
        (
            await db.execute(
                select(TenantIdempotencyRecord.id)
                .where(
                    TenantIdempotencyRecord.state == "completed",
                    TenantIdempotencyRecord.updated_at < completed_before,
                )
                .order_by(TenantIdempotencyRecord.updated_at.asc())
                .limit(limit)
            )
        ).scalars()
    )
    if record_ids:
        await db.execute(
            delete(TenantIdempotencyRecord).where(
                TenantIdempotencyRecord.id.in_(record_ids)
            )
        )
    return len(record_ids)


def _dialect_insert(db: AsyncSession, model):
    dialect = _dialect_name(db)
    if dialect == "postgresql":
        return postgresql_insert(model)
    if dialect == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"Governance counters do not support SQL dialect {dialect!r}")


def _dialect_name(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

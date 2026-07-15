from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.engine import get_engine, reset_engine_for_tests, session_scope
from app.db.models import Base
from app.governance import GovernanceLimits, GovernanceService, claim_tenant_idempotency
from app.workspace import default_workspace, set_current_workspace


POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Override the suite SQLite fixture for guarded PostgreSQL integration."""
    if not POSTGRES_URL:
        yield
        return
    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    set_current_workspace(default_workspace())
    get_settings.cache_clear()
    await reset_engine_for_tests()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await reset_engine_for_tests()
    get_settings.cache_clear()
    set_current_workspace(default_workspace())


async def test_postgres_governance_writes_are_atomic_under_concurrency() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    rate_service = GovernanceService(GovernanceLimits(requests_per_minute=5))
    decisions = await asyncio.gather(
        *(rate_service.authorize_request("ws-rate", now=now, audit=False) for _ in range(12))
    )
    assert sum(decision.allowed for decision in decisions) == 5
    assert max(decision.used for decision in decisions) == 5

    active_service = GovernanceService(GovernanceLimits(max_active_work=1))
    same = await asyncio.gather(
        active_service.acquire_active_work("ws-active", "work-same"),
        active_service.acquire_active_work("ws-active", "work-same"),
    )
    assert all(decision.allowed for decision in same)
    assert {decision.idempotent for decision in same} == {False, True}

    competing = await asyncio.gather(
        active_service.acquire_active_work("ws-compete", "work-a"),
        active_service.acquire_active_work("ws-compete", "work-b"),
    )
    assert sum(decision.allowed for decision in competing) == 1


async def test_postgres_generic_idempotency_claim_has_one_owner() -> None:
    async def claim_once():
        async with session_scope() as db:
            claim = await claim_tenant_idempotency(
                db,
                workspace_id="ws",
                operation="agent.create",
                idempotency_key="same-key",
                request_payload={"name": "agent"},
            )
            await db.commit()
            return claim

    first, second = await asyncio.gather(claim_once(), claim_once())
    assert {first.disposition, second.disposition} == {"acquired", "in_progress"}
    assert first.record_id == second.record_id

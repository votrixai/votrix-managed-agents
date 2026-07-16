import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.engine import get_engine, reset_engine_for_tests, session_scope
from app.db.models import Base, ManagedResource, SessionEvent, SessionEventIdempotency
from app.organization import (
    CurrentOrganization,
    reset_current_organization,
    set_current_organization,
)
from tests.conftest import TEST_HEADERS


POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Override the suite's SQLite fixture only for this guarded integration test."""

    if not POSTGRES_URL:
        yield
        return
    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")

    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "state")
    monkeypatch.setenv("VMA_REQUIRE_BETA_HEADER", "true")
    monkeypatch.setenv("VMA_REQUIRE_ANTHROPIC_VERSION_HEADER", "true")
    organization_token = set_current_organization(
        CurrentOrganization(id="org_test", slug="test", source="postgres_test")
    )
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
    reset_current_organization(organization_token)


@pytest.fixture
async def postgres_client():
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


async def _create_session(client: AsyncClient) -> dict:
    agent = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Postgres Idempotency Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent.status_code == 201, agent.text
    environment = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "postgres-idempotency-worker", "config": {"type": "self_hosted"}},
    )
    assert environment.status_code == 201, environment.text
    session = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent.json()["id"], "version": 1},
            "environment_id": environment.json()["id"],
        },
    )
    assert session.status_code == 201, session.text
    return session.json()


async def test_postgres_concurrent_retries_commit_one_event_and_one_work(postgres_client):
    session = await _create_session(postgres_client)
    url = f"/v1/sessions/{session['id']}/events"
    headers = {**TEST_HEADERS, "Idempotency-Key": "postgres-concurrent-turn"}
    payload = {"events": [{"type": "user.message", "content": "exactly once"}]}

    first, second = await asyncio.gather(
        postgres_client.post(url, headers=headers, json=payload),
        postgres_client.post(url, headers=headers, json=payload),
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    async with session_scope() as db:
        event_count = await db.scalar(
            select(func.count())
            .select_from(SessionEvent)
            .where(SessionEvent.session_id == session["id"], SessionEvent.type == "user.message")
        )
        work_count = await db.scalar(
            select(func.count())
            .select_from(ManagedResource)
            .where(
                ManagedResource.resource_type == "environment_work",
                ManagedResource.name == f"session:{session['id']}",
            )
        )
        submission_count = await db.scalar(
            select(func.count())
            .select_from(SessionEventIdempotency)
            .where(SessionEventIdempotency.session_id == session["id"])
        )
    assert (event_count, work_count, submission_count) == (1, 1, 1)

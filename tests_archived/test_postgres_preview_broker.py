from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.engine import reset_engine_for_tests
from app.runtime.preview_broker import PreviewBroker
from app.runtime.vma_preview_bus import VmaProcessLocalPreviewBus

POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Override the suite SQLite fixture for a guarded real NOTIFY round trip."""
    if not POSTGRES_URL:
        yield
        return
    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")

    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    get_settings.cache_clear()
    await reset_engine_for_tests()
    yield
    await reset_engine_for_tests()
    get_settings.cache_clear()


async def test_postgres_notify_delivers_foreign_preview_to_listener_bus():
    publisher_bus = VmaProcessLocalPreviewBus()
    listener_bus = VmaProcessLocalPreviewBus()
    publisher = PreviewBroker(
        instance_id="publisher-a",
        local_bus=publisher_bus,
        mode="pg_notify",
        database_url=POSTGRES_URL,
        service_role="worker",
    )
    listener = PreviewBroker(
        instance_id="listener-b",
        local_bus=listener_bus,
        mode="pg_notify",
        database_url=POSTGRES_URL,
        service_role="api",
    )
    frame = {
        "type": "event_start",
        "event": {"type": "agent.message", "id": "evt_postgres_preview"},
    }
    try:
        await listener.start()
        assert await listener.wait_until_listener_ready(timeout=10)
        await publisher.start()
        async with listener_bus.subscribe(
            "sess_postgres_preview",
            organization_id="org_postgres_preview",
        ) as queue:
            await publisher.publish(
                "sess_postgres_preview",
                frame,
                organization_id="org_postgres_preview",
            )
            received = await asyncio.wait_for(queue.get(), timeout=10)
        assert received == frame
    finally:
        await publisher.close()
        await listener.close()

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import asyncpg
import pytest

from app.config import get_settings
from app.runtime.preview_broker import PreviewBroker
from app.runtime.vma_preview_bus import VmaProcessLocalPreviewBus


def _start(event_id: str = "evt_1") -> dict[str, Any]:
    return {
        "type": "event_start",
        "event": {"type": "agent.message", "id": event_id},
    }


def _delta(
    text: str,
    *,
    event_id: str = "evt_1",
    index: int = 0,
) -> dict[str, Any]:
    return {
        "type": "event_delta",
        "event_id": event_id,
        "delta": {
            "type": "content_delta",
            "index": index,
            "content": {"type": "text", "text": text},
        },
    }


def _remote_frames(notifications: list[str]) -> list[dict[str, Any]]:
    return [json.loads(item)["f"] for item in notifications]


@pytest.fixture
async def broker_factory():
    brokers: list[PreviewBroker] = []

    async def notify_noop(_payload: str) -> None:
        return None

    def create(**kwargs: Any) -> PreviewBroker:
        kwargs.setdefault("notify_sink", notify_noop)
        broker = PreviewBroker(
            mode="pg_notify",
            database_url="postgresql+asyncpg://preview.invalid/vma_test",
            service_role="worker",
            **kwargs,
        )
        brokers.append(broker)
        return broker

    yield create

    await asyncio.gather(*(broker.close() for broker in brokers))


async def test_process_local_mode_is_byte_identical_and_never_notifies():
    local_bus = VmaProcessLocalPreviewBus()
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = PreviewBroker(
        instance_id="local",
        local_bus=local_bus,
        mode="process_local",
        database_url="sqlite+aiosqlite:///preview.db",
        notify_sink=notify,
    )
    frame = _delta("local")
    async with local_bus.subscribe("sess_1", organization_id="org_1") as queue:
        delivered = await broker.publish("sess_1", frame, organization_id="org_1")
        received = await asyncio.wait_for(queue.get(), timeout=1)
    await broker.close()

    assert delivered == 1
    assert received == frame
    assert notifications == []


async def test_coalescer_merges_only_adjacent_matching_text_deltas_and_preserves_fifo(
    broker_factory: Callable[..., PreviewBroker],
):
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = broker_factory(instance_id="publisher", notify_sink=notify)
    await broker.start()
    await broker.publish("sess_1", _delta("A"), organization_id="org_1")
    await broker.publish("sess_1", _delta("B"), organization_id="org_1")
    await broker.publish(
        "sess_1",
        _delta("C", event_id="evt_2"),
        organization_id="org_1",
    )
    await broker.publish("sess_1", _delta("D"), organization_id="org_1")
    await broker.publish(
        "sess_1",
        _delta("E", index=1),
        organization_id="org_1",
    )
    await broker.flush_pending()

    frames = _remote_frames(notifications)
    assert [(frame["event_id"], frame["delta"]["index"]) for frame in frames] == [
        ("evt_1", 0),
        ("evt_2", 0),
        ("evt_1", 0),
        ("evt_1", 1),
    ]
    assert [frame["delta"]["content"]["text"] for frame in frames] == [
        "AB",
        "C",
        "D",
        "E",
    ]


async def test_event_start_flushes_its_session_immediately(
    broker_factory: Callable[..., PreviewBroker],
):
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = broker_factory(instance_id="publisher", notify_sink=notify)
    await broker.start()
    await broker.publish("sess_1", _start(), organization_id="org_1")

    assert _remote_frames(notifications) == [_start()]


async def test_coalescer_flushes_small_frame_after_fifty_milliseconds(
    broker_factory: Callable[..., PreviewBroker],
):
    notifications: list[str] = []
    notified = asyncio.Event()

    async def notify(payload: str) -> None:
        notifications.append(payload)
        notified.set()

    broker = broker_factory(instance_id="publisher", notify_sink=notify)
    await broker.start()
    await broker.publish("sess_1", _delta("small"), organization_id="org_1")
    await asyncio.sleep(0.025)
    assert notifications == []

    await asyncio.wait_for(notified.wait(), timeout=0.15)
    assert _remote_frames(notifications) == [_delta("small")]


async def test_coalescer_flushes_when_session_buffer_reaches_four_kibibytes(
    broker_factory: Callable[..., PreviewBroker],
):
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = broker_factory(instance_id="publisher", notify_sink=notify)
    await broker.start()
    large_delta = _delta("x" * 4_100)
    await broker.publish("sess_1", large_delta, organization_id="org_1")

    assert _remote_frames(notifications) == [large_delta]


async def test_oversized_remote_payload_is_dropped_whole_after_local_delivery(
    broker_factory: Callable[..., PreviewBroker],
):
    local_bus = VmaProcessLocalPreviewBus()
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = broker_factory(
        instance_id="publisher",
        local_bus=local_bus,
        notify_sink=notify,
    )
    await broker.start()
    oversized = _delta("x" * 7_500)
    async with local_bus.subscribe("sess_1", organization_id="org_1") as queue:
        await broker.publish("sess_1", oversized, organization_id="org_1")
        received = await asyncio.wait_for(queue.get(), timeout=1)

    assert received == oversized
    assert notifications == []


async def test_non_json_remote_payload_is_dropped_after_local_delivery(
    broker_factory: Callable[..., PreviewBroker],
):
    local_bus = VmaProcessLocalPreviewBus()
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = broker_factory(
        instance_id="publisher",
        local_bus=local_bus,
        notify_sink=notify,
    )
    await broker.start()
    frame = {**_start(), "non_json": {"not", "serializable"}}
    async with local_bus.subscribe("sess_1", organization_id="org_1") as queue:
        delivered = await broker.publish("sess_1", frame, organization_id="org_1")
        received = await asyncio.wait_for(queue.get(), timeout=1)

    assert delivered == 1
    assert received == frame
    assert notifications == []


async def test_loopback_notification_is_suppressed_after_exactly_one_local_delivery(
    broker_factory: Callable[..., PreviewBroker],
):
    local_bus = VmaProcessLocalPreviewBus()
    broker: PreviewBroker

    async def loopback_notify(payload: str) -> None:
        assert await broker.handle_notification(payload) is False

    broker = broker_factory(
        instance_id="same-process",
        local_bus=local_bus,
        notify_sink=loopback_notify,
    )
    await broker.start()
    frame = _start()
    async with local_bus.subscribe("sess_1", organization_id="org_1") as queue:
        delivered = await broker.publish("sess_1", frame, organization_id="org_1")
        assert await asyncio.wait_for(queue.get(), timeout=1) == frame
        await asyncio.sleep(0)
        assert queue.empty()

    assert delivered == 1


async def test_foreign_notification_republishes_to_tenant_scoped_local_topic(
    broker_factory: Callable[..., PreviewBroker],
):
    local_bus = VmaProcessLocalPreviewBus()
    broker = broker_factory(instance_id="listener-b", local_bus=local_bus)
    raw_payload = json.dumps(
        {"i": "publisher-a", "o": "org_1", "s": "sess_1", "f": _delta("remote")},
        separators=(",", ":"),
    )
    async with local_bus.subscribe("sess_1", organization_id="org_1") as queue:
        assert await broker.handle_notification(raw_payload) is True
        assert await asyncio.wait_for(queue.get(), timeout=1) == _delta("remote")

    assert await broker.handle_notification("not-json") is False


async def test_close_flushes_pending_frames(
    broker_factory: Callable[..., PreviewBroker],
):
    notifications: list[str] = []

    async def notify(payload: str) -> None:
        notifications.append(payload)

    broker = broker_factory(instance_id="publisher", notify_sink=notify)
    await broker.start()
    await broker.publish("sess_1", _delta("shutdown"), organization_id="org_1")
    assert notifications == []

    await broker.close()
    assert _remote_frames(notifications) == [_delta("shutdown")]


async def test_pg_notify_with_non_postgres_database_fails_fast():
    broker = PreviewBroker(
        mode="pg_notify",
        database_url="sqlite+aiosqlite:///preview.db",
        service_role="api",
    )
    with pytest.raises(RuntimeError, match="requires a PostgreSQL DATABASE_URL") as exc_info:
        await broker.start()
    assert "VMA_LISTEN_DATABASE_URL" in str(exc_info.value)
    await broker.close()


async def test_pg_notify_requires_postgres_publisher_with_split_dsns(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///preview.db")
    monkeypatch.setenv(
        "VMA_LISTEN_DATABASE_URL",
        "postgresql+asyncpg://session.example:5432/vma",
    )
    get_settings.cache_clear()
    broker = PreviewBroker(mode="pg_notify", service_role="worker")

    with pytest.raises(RuntimeError, match="PostgreSQL DATABASE_URL for publishing"):
        await broker.start()
    await broker.close()


async def test_worker_role_never_starts_listener(
    broker_factory: Callable[..., PreviewBroker],
):
    broker = broker_factory(instance_id="worker")
    assert await broker.start() is None
    assert broker.listener_task is None


async def test_listener_reconnects_and_closes_dedicated_connection(
    monkeypatch,
):
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False
            self.listener_added = False
            self.listener_removed = False

        async def add_listener(self, _channel, _callback) -> None:
            self.listener_added = True

        async def remove_listener(self, _channel, _callback) -> None:
            self.listener_removed = True

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    connect_attempts = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts == 1:
            raise ConnectionError("temporary listener outage")
        return connection

    async def notify_noop(_payload: str) -> None:
        return None

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    broker = PreviewBroker(
        instance_id="listener",
        mode="pg_notify",
        database_url="postgresql+asyncpg://preview.invalid/vma_test",
        service_role="api",
        notify_sink=notify_noop,
        reconnect_initial_seconds=0.001,
        reconnect_max_seconds=0.002,
        connection_check_seconds=0.005,
    )
    task = await broker.start()
    assert task is not None
    assert await broker.wait_until_listener_ready(timeout=0.25)
    assert connect_attempts == 2
    assert connection.listener_added is True

    await broker.close()
    assert task.done()
    assert connection.listener_removed is True
    assert connection.closed is True


@pytest.mark.parametrize(
    ("listen_url", "expected_dsn"),
    [
        (
            "postgresql+asyncpg://session.example:5432/vma",
            "postgresql://session.example:5432/vma",
        ),
        ("", "postgresql://transaction.example:6543/vma"),
    ],
)
async def test_listener_uses_session_scoped_dsn_with_fallback(
    monkeypatch,
    listen_url,
    expected_dsn,
):
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        async def add_listener(self, _channel, _callback) -> None:
            return None

        async def remove_listener(self, _channel, _callback) -> None:
            return None

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    connected_dsns: list[str] = []
    connection = FakeConnection()

    async def fake_connect(dsn, **_kwargs):
        connected_dsns.append(dsn)
        return connection

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://transaction.example:6543/vma",
    )
    monkeypatch.setenv("VMA_LISTEN_DATABASE_URL", listen_url)
    get_settings.cache_clear()
    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    broker = PreviewBroker(
        instance_id="session-listener",
        mode="pg_notify",
        service_role="api",
        connection_check_seconds=0.005,
    )

    task = await broker.start()
    assert task is not None
    assert await broker.wait_until_listener_ready(timeout=0.25)
    await broker.close()

    assert connected_dsns == [expected_dsn]

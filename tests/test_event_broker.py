"""Waking a stream instead of having it ask.

The connection itself is not exercised here: what a real `LISTEN` does is the
driver's business, and faking a Postgres that delivers notifications would only
test the fake. What is exercised is everything this module decides on its own —
who gets woken, what happens when nobody is listening, and whether a stream
that has come and gone leaves anything behind.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import get_settings
from app.db.queries.sessions import event_channel
from app.services import event_broker


@pytest.fixture(autouse=True)
async def clean_broker():
    """No test inherits another's waiters, listener or readiness."""
    yield
    await event_broker.close()
    event_broker._ready = False


@pytest.fixture
async def listening(monkeypatch):
    """A broker that believes it is connected, without connecting to anything.

    The stand-in listener is a real task because that is what the code checks:
    a broker whose listener has finished starts another one.
    """

    async def _never_finishes():
        await asyncio.sleep(3600)

    listener = asyncio.create_task(_never_finishes())
    monkeypatch.setattr(event_broker, "_ready", True)
    monkeypatch.setattr(event_broker, "_listener", listener)
    monkeypatch.setenv("VMA_LISTEN_DATABASE_URL", "postgresql://listener/db")
    get_settings.cache_clear()
    yield
    listener.cancel()
    get_settings.cache_clear()


# --- with nobody to tell it --------------------------------------------------


async def test_an_unconfigured_broker_just_polls(monkeypatch):
    """The whole fallback contract in one test: no listener, no behaviour change.

    Local development and the worker service both run this way, and so does
    production for as long as it takes a dropped connection to come back.
    """
    monkeypatch.setenv("VMA_LISTEN_DATABASE_URL", "")
    get_settings.cache_clear()

    started = time.monotonic()
    await event_broker.wait("sess_quiet", poll_interval=0.05)

    assert time.monotonic() - started >= 0.05
    # Nothing was started to do it, which is what keeps the worker service off
    # the session-mode connection budget.
    assert event_broker._listener is None
    get_settings.cache_clear()


async def test_a_lost_notification_still_ends_the_wait(listening, monkeypatch):
    """Nothing arrives, and the stream reads anyway.

    This is what keeps a dropped notification a latency cost rather than a
    stream that hangs: the timeout is a safety net, not a promise that one was
    sent. Note which interval it uses — the caller's poll interval is ten
    seconds here and does not apply once there is a listener.
    """
    monkeypatch.setattr(event_broker, "NOTIFIED_TIMEOUT_SECONDS", 0.05)

    started = time.monotonic()
    await event_broker.wait("sess_quiet", poll_interval=10.0)

    assert time.monotonic() - started < 1.0


# --- who gets woken ----------------------------------------------------------


async def test_a_notification_wakes_the_session_it_names(listening):
    waiting = asyncio.create_task(event_broker.wait("sess_watched", poll_interval=10.0))
    await _until_registered("sess_watched")

    event_broker._on_notify(None, 0, event_channel(), "sess_watched")

    await asyncio.wait_for(waiting, timeout=1.0)


async def test_a_notification_leaves_other_sessions_asleep(listening):
    """Every instance is woken by every session's events, because there is one
    channel. Dropping the ones nobody here is reading is the whole of the fan-out.
    """
    waiting = asyncio.create_task(event_broker.wait("sess_mine", poll_interval=10.0))
    await _until_registered("sess_mine")

    event_broker._on_notify(None, 0, event_channel(), "sess_somebody_elses")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiting), timeout=0.1)
    waiting.cancel()


async def test_every_reader_of_one_session_is_woken(listening):
    """Two browser tabs on the same conversation are two waiters."""
    first = asyncio.create_task(event_broker.wait("sess_shared", poll_interval=10.0))
    second = asyncio.create_task(event_broker.wait("sess_shared", poll_interval=10.0))
    await _until_registered("sess_shared", count=2)

    event_broker._on_notify(None, 0, event_channel(), "sess_shared")

    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)


# --- what is left behind -----------------------------------------------------


async def test_a_finished_stream_leaves_no_entry(listening):
    """The registry is keyed by session, and a service that streamed a million
    of them should not still be holding a million keys."""
    waiting = asyncio.create_task(event_broker.wait("sess_transient", poll_interval=10.0))
    await _until_registered("sess_transient")

    event_broker._on_notify(None, 0, event_channel(), "sess_transient")
    await asyncio.wait_for(waiting, timeout=1.0)

    assert "sess_transient" not in event_broker._waiters


async def test_a_cancelled_stream_leaves_no_entry(listening):
    """A client that closes the tab cancels the generator mid-wait."""
    waiting = asyncio.create_task(event_broker.wait("sess_abandoned", poll_interval=10.0))
    await _until_registered("sess_abandoned")

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert "sess_abandoned" not in event_broker._waiters


# --- the channel -------------------------------------------------------------


def test_the_channel_carries_the_schema(monkeypatch):
    """Staging and production can share one Postgres, and a notification is not
    scoped by `search_path` — without this they would wake each other's readers.
    """
    monkeypatch.setenv("DATABASE_SCHEMA", "vma_staging")
    get_settings.cache_clear()
    try:
        assert event_channel() == "vma_session_events_vma_staging"
    finally:
        get_settings.cache_clear()


def test_the_channel_has_a_name_without_a_schema():
    assert event_channel() == "vma_session_events_public"


# --- the DSN ------------------------------------------------------------------


def test_the_configured_dsn_is_handed_to_asyncpg_in_a_shape_it_accepts():
    """`1-create-secrets.sh` requires the setting in SQLAlchemy's form, and
    asyncpg refuses that form. Getting this wrong connects nothing, forever,
    while every stream falls back to polling and the logs fill quietly."""
    configured = "postgresql+asyncpg://postgres.ref:pw@aws-0.pooler.supabase.com:5432/postgres"

    assert event_broker.asyncpg_dsn(configured) == (
        "postgresql://postgres.ref:pw@aws-0.pooler.supabase.com:5432/postgres"
    )


async def test_asyncpg_really_does_reject_the_configured_form():
    """The reason the function above exists, pinned against the driver itself
    rather than against a memory of what it does.

    The DSN is parsed before anything is dialled, so this refuses without
    touching the network — and it refuses with its own error type rather than
    a connection failure, which is what makes the assertion meaningful.
    """
    import asyncpg

    with pytest.raises(asyncpg.exceptions.ClientConfigurationError) as refused:
        await asyncpg.connect("postgresql+asyncpg://u:p@h:5432/db", timeout=0.1)

    assert "scheme" in str(refused.value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h:5432/db",
        "postgres://u:p@h:5432/db",
    ],
)
def test_a_plain_dsn_is_left_alone(url):
    assert event_broker.asyncpg_dsn(url) == url


def test_something_that_is_not_a_url_is_passed_through_untouched():
    """Not this function's job to validate: asyncpg reports a bad DSN better
    than a guess here would."""
    assert event_broker.asyncpg_dsn("nonsense") == "nonsense"


async def _until_registered(session_id: str, *, count: int = 1) -> None:
    """Let the waiting task reach the point of being registered."""
    for _ in range(100):
        if len(event_broker._waiters.get(session_id, ())) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{session_id} never registered {count} waiter(s)")

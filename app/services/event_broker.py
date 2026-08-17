"""The doorbell for `session_events`.

A stream and the turn it is following are never in the same process: the turn
runs on the worker service, the stream is served by whichever API instance the
client's connection happened to land on. What joins them is the events table —
which, before this module, a stream re-read every 300ms whether or not anything
had been written to it.

So a writer sends the session id through Postgres `NOTIFY`, inside the same
transaction that appends the event, and the process holding the stream wakes up
and runs the read it was going to run anyway.

The read stays authoritative, and that is the whole of the design. A
notification carries no event data, so nothing here can deliver an event late,
twice, or out of order — the worst a lost one costs is a single fallback
interval of extra latency. Postgres is still the only source of truth; this
changes when it is asked, never what it answers.

That contract is also the fallback: with no listener configured, or none
working, every stream simply polls the way it always did.

Previews are the exception, on a second channel, and they invert every sentence
above: the notification *is* the data, nothing is written down, and a lost one
is text no reader will ever see. That is affordable only because of what a
preview is — the words of a message still being written, made worthless by the
`agent.message` that follows a few seconds later. Nothing downstream may treat
one as a fact, which is why they are kept out of the log entirely rather than
written and hidden.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import structlog
from sqlalchemy import text

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries.sessions import delta_channel, event_channel

logger = structlog.get_logger(__name__)

# How long a woken-up stream will wait before reading anyway. This is only a
# safety net for a notification that went missing — the poll interval the
# caller passes in is what a stream falls back to when there is no listener at
# all, and that one has to stay short enough to feel live.
NOTIFIED_TIMEOUT_SECONDS = 5.0

CONNECT_TIMEOUT_SECONDS = 10.0
# Long enough to cross the network and come back, short enough that a listener
# pointed at the wrong port is discovered at startup rather than in production.
SELF_TEST_SECONDS = 2.0
RECONNECT_DELAY_SECONDS = 5.0

# Which streams are waiting on which session. Every entry belongs to a
# connection somebody is holding open, so entries leave as soon as they stop
# being waited on.
_waiters: dict[str, set[asyncio.Event]] = {}

# The same shape for previews, and a queue rather than an event because these
# carry their text with them: there is no table to go back and read, so a
# notification that arrives with nowhere to put it is text nobody will see.
_delta_subs: dict[str, set["DeltaSubscription"]] = {}

# How many previews may be in front of a stream before the rest are dropped.
# Deep enough that a client which stalls for a second catches up, shallow enough
# that one which has stopped reading cannot make this process hold a turn's
# entire output. Dropping is the correct outcome and not a failure: a preview is
# worth showing while the model is still talking and worthless afterwards, so a
# reader far enough behind to overflow this is better served by the
# `agent.message` it is about to get anyway.
DELTA_QUEUE_DEPTH = 64

_listener: asyncio.Task | None = None
_start_lock = asyncio.Lock()
_ready = False


async def wait(
    session_id: str,
    *,
    poll_interval: float,
    previews: "DeltaSubscription | None" = None,
) -> None:
    """Block until something happens for this session, or until we give up.

    Returns either way, and says nothing about which happened: the caller drains
    its previews and runs its query regardless, and a missed notification has to
    be indistinguishable from a quiet session — anything else would be a way for
    a reader to conclude there is nothing to fetch when there is.

    Two things can end the wait, and they are not the same kind of thing. An
    event was written, which means there is a row to go and read; or a preview
    arrived, which means the text is already in hand and there is nothing to
    read. Waking on both from one call is what keeps a stream from having to
    poll the table at the pace previews arrive — twice a second, for a table
    that has nothing new in it between messages.
    """
    if not await _ensure_listening():
        await asyncio.sleep(poll_interval)
        return

    woken = asyncio.Event()
    _waiters.setdefault(session_id, set()).add(woken)
    bells = [woken.wait()]
    if previews is not None:
        bells.append(previews.arrived.wait())
    tasks = [asyncio.ensure_future(bell) for bell in bells]
    try:
        await asyncio.wait(
            tasks,
            timeout=NOTIFIED_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Cancelled unconditionally, and in the `finally` rather than after the
        # `await`. This function is cancelled routinely — it is what a client
        # closing a tab does — and a cancellation lands *inside* the wait above,
        # so anything written after it never runs. Whichever bell did not ring
        # would then be left pending forever with this frame gone.
        for task in tasks:
            task.cancel()
        # Then collected, so a cancelled task is not reported as never awaited
        # on some later, unrelated tick. `return_exceptions` keeps the
        # `CancelledError` each one raises from replacing the cancellation this
        # function is already unwinding under — never suppress that one: it is
        # how the caller learns it was cancelled at all.
        await asyncio.gather(*tasks, return_exceptions=True)
        waiting = _waiters.get(session_id)
        if waiting is not None:
            waiting.discard(woken)
            # An empty set left behind would keep the session id for the life of
            # the process, and this dictionary is keyed by every session anyone
            # has ever streamed.
            if not waiting:
                _waiters.pop(session_id, None)


class DeltaSubscription:
    """One stream's view of the preview channel: what arrived, and a bell.

    The bell is separate from the queue because the two answer different
    questions and only one of them can be asked without consuming anything.
    `wait` needs to know *that* something arrived so it can stop blocking;
    draining needs to know *what*. Using `Queue.get()` for both would make
    waiting destructive — the item that woke the sleeper would be held by the
    sleeper, out of the order the drain is about to read.
    """

    __slots__ = ("queue", "arrived")

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=DELTA_QUEUE_DEPTH)
        self.arrived = asyncio.Event()

    def drain(self) -> list[tuple[str, str]]:
        """Take everything waiting, and re-arm the bell.

        Clearing before draining, not after: a notification landing between the
        two is one whose item is still in the queue when the next drain runs,
        so at worst it is read a moment late. The other order can lose one — the
        bell would be cleared after the item was taken, silencing an arrival
        that happened in between.
        """
        self.arrived.clear()
        taken: list[tuple[str, str]] = []
        while True:
            try:
                taken.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return taken


@contextlib.asynccontextmanager
async def deltas(session_id: str) -> AsyncIterator[DeltaSubscription]:
    """Subscribe to this session's previews for as long as the block runs.

    A context manager rather than a subscribe/unsubscribe pair because the
    caller is an async generator that a client can abandon at any await. If the
    unsubscribe were a statement someone had to remember, every dropped
    connection would leave a queue behind that the listener goes on filling, for
    the life of the process.

    Yields a subscription even when there is no listener configured. The stream
    is then a stream with no previews in it, which is a working stream — making
    this optional at the call site would put that branch in the caller instead.
    """
    await _ensure_listening()
    subscription = DeltaSubscription()
    _delta_subs.setdefault(session_id, set()).add(subscription)
    try:
        yield subscription
    finally:
        subscribed = _delta_subs.get(session_id)
        if subscribed is not None:
            subscribed.discard(subscription)
            if not subscribed:
                _delta_subs.pop(session_id, None)


async def close() -> None:
    """Stop listening, and forget everything that was waiting."""
    global _listener, _ready
    _ready = False
    if _listener is not None:
        _listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _listener
        _listener = None
    _waiters.clear()
    _delta_subs.clear()


def asyncpg_dsn(url: str) -> str:
    """Drop SQLAlchemy's driver suffix, which asyncpg refuses outright.

    The setting is written `postgresql+asyncpg://…` because that is the shape
    every other DSN in this service has, and `scripts/gcloud/1-create-secrets.sh`
    validates it in exactly that form. asyncpg's own parser accepts only
    `postgresql` or `postgres` and raises `ClientConfigurationError` on anything
    else — so handing the setting straight over connects nothing, forever,
    while every stream quietly falls back to polling.
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        return url
    return f"{scheme.split('+', 1)[0]}{separator}{rest}"


async def _ensure_listening() -> bool:
    """Start the listener on first use, and report whether it is usable yet.

    Lazily rather than at boot because one image serves two Cloud Run services
    and only one of them streams. The worker never reaches this, so it never
    spends a session-mode connection on a channel it would not read — which is
    exactly the connection budget the scaling runbook allows it.

    A caller that arrives while the connection is still being made is told no
    and polls once. Waiting for it here would put the cost of the handshake on
    whichever stream happened to be first.
    """
    global _listener
    if not get_settings().vma_listen_database_url:
        return False
    if _listener is None or _listener.done():
        async with _start_lock:
            if _listener is None or _listener.done():
                _listener = asyncio.create_task(_listen_forever())
    return _ready


async def _listen_forever() -> None:
    """Hold one connection open on the channel, and replace it when it drops.

    A dropped listener is the failure that would otherwise be invisible: every
    stream would go on working, on the fallback timeout, for as long as the
    process lived. Clearing `_ready` on the way out is what sends them back to
    polling until there is really a connection again.
    """
    global _ready
    import asyncpg

    channel = event_channel()
    while True:
        connection = None
        try:
            connection = await asyncpg.connect(
                asyncpg_dsn(get_settings().vma_listen_database_url),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            await connection.add_listener(channel, _on_notify)
            # One connection, both channels. They are separate channels because
            # they carry different things, not because they need separate
            # sockets — and a session-mode connection is scarce enough that
            # spending a second one to listen to the same database would come
            # out of the same fifteen-client ceiling.
            await connection.add_listener(delta_channel(), _on_delta)
            if not await _round_trips(connection, channel):
                raise RuntimeError(
                    f"LISTEN {channel} is connected but delivers nothing, which "
                    "is what a transaction-mode pooler does — the listener needs "
                    "a session-mode connection"
                )
            _ready = True
            logger.info("event_listener_ready", channel=channel)
            while not connection.is_closed():
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
            # Waited before trying again, like the failure path below: a
            # connection that is accepted and then dropped straight away would
            # otherwise be reconnected as fast as the network allows.
            logger.warning("event_listener_disconnected", channel=channel)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event_listener_failed", channel=channel)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            _ready = False
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.close(timeout=CONNECT_TIMEOUT_SECONDS)


async def _round_trips(connection, channel: str) -> bool:
    """Prove the channel delivers before trusting anything to it.

    `LISTEN` succeeds against a transaction-mode pooler and then silently never
    delivers, which would leave every stream running on the fallback timeout
    with nothing in the logs to say why. Sending ourselves one notification
    turns a config mistake that degrades quietly into one that fails loudly.

    The probe is published the way a real event is — through the main engine,
    which in a deployment is the *transaction* pooler — and not through the
    listener's own connection. Testing the listener against itself would prove
    only that session mode delivers to session mode, which was never in doubt.
    What has to be established is that a notification issued on a pooled
    transaction reaches a listener holding a session-mode connection, because
    that is the path every event takes and it is the one this deployment has
    never exercised. Getting it wrong is invisible from the listener's side:
    the connection is healthy, the channel is right, and nothing ever arrives.
    """
    heard = asyncio.Event()
    probe = f"self-test-{id(heard)}"

    def _on_probe(_connection, _pid, _channel, payload) -> None:
        if payload == probe:
            heard.set()

    await connection.add_listener(channel, _on_probe)
    try:
        # Committed, because Postgres holds a notification until the transaction
        # that issued it commits — the same property `append_event` relies on.
        async with session_scope() as db:
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": channel, "payload": probe},
            )
            await db.commit()
        await asyncio.wait_for(heard.wait(), timeout=SELF_TEST_SECONDS)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        with contextlib.suppress(Exception):
            await connection.remove_listener(channel, _on_probe)


def _on_notify(_connection, _pid, _channel, payload) -> None:
    """Wake every stream watching that session.

    Called by asyncpg on the event loop, so the set cannot be mutated underneath
    this loop — but it is copied anyway, because what a waiter does when woken
    is to take itself out of that set.
    """
    for woken in tuple(_waiters.get(payload, ())):
        woken.set()


def _on_delta(_connection, _pid, _channel, payload) -> None:
    """Hand one preview to every stream subscribed to its session.

    Runs on the event loop inside asyncpg's reader, which is why nothing here
    blocks or awaits: `put_nowait` either takes the item or raises, and raising
    is the intended outcome for a reader that has fallen far enough behind to
    fill its queue. Waiting for space instead would stall the connection that
    delivers notifications for the whole process.

    A payload this cannot parse is discarded silently. The alternative is an
    exception inside a callback asyncpg gives nowhere to go, and there is no
    recovery to attempt — the text is already gone.
    """
    try:
        message = json.loads(payload)
        session_id = message["s"]
        frame = (message["t"], message["x"])
    except (ValueError, KeyError, TypeError):
        logger.warning("event_listener_bad_delta")
        return

    for subscription in tuple(_delta_subs.get(session_id, ())):
        try:
            subscription.queue.put_nowait(frame)
        except asyncio.QueueFull:
            continue
        subscription.arrived.set()

"""Durable handoff, restart recovery, and delivery acknowledgement boundaries."""

import asyncio
import copy
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import worker
from app.config import get_settings
from app.db.models import Base, SessionEvent, SessionTurn, WorkerPoolControl
from app.db.queries import sessions as sessions_q
from app.models.errors import SessionBusy
from app.runtime import engine
from app.runtime.recovery import Recovery, RecoveryRequired
from app.services import pubsub, sessions, worker_pool
from app.services import turn_execution as turns

MESSAGE = {"type": "user.message", "content": [{"type": "text", "text": "hello"}]}
REAL_DISPATCH = sessions._dispatch_turn


@pytest_asyncio.fixture
async def db():
    """Opt into isolated PostgreSQL schemas for real row-lock tests."""
    url = os.getenv("VMA_TEST_POSTGRES_URL")
    schema = "test_turns_" + uuid4().hex
    options = (
        {"connect_args": {"server_settings": {"search_path": schema}}} if url else {}
    )
    database = create_async_engine(url or "sqlite+aiosqlite:///:memory:", **options)
    async with database.begin() as conn:
        if url:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(database, expire_on_commit=False)() as db:
            yield db
    finally:
        if url:
            async with database.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await database.dispose()


@pytest.fixture
def dispatch(monkeypatch, db):
    for name, value in {
        "TURN_DISPATCH": "pubsub",
        "PUBSUB_PROJECT": "test",
        "PUBSUB_TOPIC": "turns",
        "PUBSUB_SUBSCRIPTION": "worker",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with factory() as connection:
            yield connection

    monkeypatch.setattr(turns, "session_scope", scope)
    monkeypatch.setattr(sessions, "session_scope", scope)
    monkeypatch.setattr(worker, "session_scope", scope)
    return factory


async def accepted(db, session):
    await sessions.send_events(
        db,
        session_id=session.id,
        organization_id=session.organization_id,
        events=[MESSAGE],
    )
    return session.id, session.lock_version


async def retry_ready(db):
    await db.execute(
        update(SessionTurn).values(
            available_at=turns.now() - timedelta(seconds=1), retry_after=None
        )
    )
    await db.commit()


async def test_failed_publish_remains_accepted_and_is_republished(
    db, session, dispatch, monkeypatch
):
    publish = AsyncMock(side_effect=RuntimeError("Pub/Sub unavailable"))
    monkeypatch.setattr(pubsub, "publish_turn", publish)
    monkeypatch.setattr(sessions, "_dispatch_turn", REAL_DISPATCH)
    key = await accepted(db, session)
    await db.refresh(session)
    assert session.status == "running"
    assert (await db.get(SessionTurn, key)).events == [MESSAGE]
    await db.commit()
    publish.side_effect = None
    assert await turns.recover_once() == 1
    assert publish.await_count == 2
    assert await turns.recover_once() == 0


async def test_queue_wait_is_not_worker_loss(db, session, dispatch):
    await accepted(db, session)
    session.lease_expires_at = turns.now() - timedelta(hours=2)
    await db.commit()
    assert await worker.sweep_once() == 0
    with pytest.raises(SessionBusy):
        await accepted(db, session)


async def test_republished_retry_can_be_acquired(db, session, dispatch, monkeypatch):
    key = await accepted(db, session)
    first = await turns.acquire(*key)
    await turns._record_failure(first, httpx.ReadError("lost"))
    assert await turns.acquire(*key) is None
    await retry_ready(db)
    monkeypatch.setattr(pubsub, "publish_turn", AsyncMock())
    assert await turns.recover_once() == 1
    # The scan reserves the next publish window; that must not postpone the
    # already-due execution again and create an endless republish/ack loop.
    assert (await turns.acquire(*key)).attempts == 2


async def test_duplicate_delivery_and_stale_owner_are_fenced(db, session, dispatch):
    key = await accepted(db, session)
    first = await turns.acquire(*key)
    assert await turns.acquire(*key) is None
    await db.execute(
        update(SessionTurn).values(lease_until=turns.now() - timedelta(seconds=1))
    )
    await db.commit()
    second = await turns.acquire(*key)
    assert second.owner != first.owner
    assert second.attempts == 2
    with pytest.raises(turns.LeaseLost):
        await first.finish({"type": "end_turn"})
    with pytest.raises(turns.LeaseLost):
        await first.emit("late", "agent.message", {"content": []})
    with pytest.raises(turns.LeaseLost):
        async with first.guard():
            pytest.fail("stale checkpoint writer entered")
    await second.finish({"type": "end_turn"})
    assert await turns.acquire(*key) is None


@pytest.mark.postgres
async def test_two_workers_cannot_acquire_the_same_turn(db, session, dispatch):
    if db.bind.dialect.name != "postgresql":
        pytest.skip("set VMA_TEST_POSTGRES_URL for real row-lock contention")
    key = await accepted(db, session)
    results = await asyncio.gather(turns.acquire(*key), turns.acquire(*key))
    assert sum(result is not None for result in results) == 1


async def test_interrupt_fences_old_turn_and_allows_a_new_one(db, session, dispatch):
    key = await accepted(db, session)
    owner = await turns.acquire(*key)
    await sessions.cancel_session(
        db, session_id=session.id, organization_id=session.organization_id
    )
    next_key = await accepted(db, session)
    assert next_key != key
    assert await turns.acquire(*key) is None
    with pytest.raises(turns.LeaseLost):
        await owner.finish({"type": "end_turn"})
    assert await turns.acquire(*next_key) is not None


async def test_retry_limit_ends_the_turn(db, session, dispatch, monkeypatch):
    key = await accepted(db, session)
    monkeypatch.setattr(
        sessions, "process_session", AsyncMock(side_effect=httpx.ReadError("lost"))
    )
    for _ in range(3):
        await retry_ready(db)
        await turns.run_turn(*key)
    await db.refresh(session)
    assert session.status == "idle"
    assert session.stop_reason == {"type": "error"}
    assert (await db.get(SessionTurn, key, populate_existing=True)).done


async def test_shutdown_leaves_recoverable_lease(db, session, dispatch, monkeypatch):
    key = await accepted(db, session)
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sessions, "process_session", slow)
    task = asyncio.create_task(turns.run_turn(*key))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    turn = await db.get(SessionTurn, key, populate_existing=True)
    assert not turn.done and turn.owner and not turns.expired(turn.lease_until)
    assert await turns.acquire(*key) is None


async def test_pubsub_execution_outlives_the_legacy_deadline(
    db, session, dispatch, monkeypatch
):
    # Scale the old deadline down so this detects accidental reuse without a
    # twenty-minute test. The actual runner, lease and completion path execute.
    monkeypatch.setattr(sessions, "TURN_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        sessions_q,
        "get_sandbox",
        AsyncMock(
            return_value=SimpleNamespace(
                deleted_at=None, state="running", external_sandbox_id="test-sandbox"
            )
        ),
    )
    monkeypatch.setattr(sessions.Sandbox, "from_id", lambda *args: object())
    monkeypatch.setattr(sessions, "collect_outputs", AsyncMock())
    monkeypatch.setattr(
        sessions.accounts_service,
        "resolve_spendable_key",
        AsyncMock(return_value="test"),
    )

    async def slow_agent(**kwargs):
        assert isinstance(kwargs["recovery"], Recovery)
        await asyncio.sleep(0.03)
        return {"type": "end_turn"}

    monkeypatch.setattr(engine, "execute_agent", slow_agent)
    key = await accepted(db, session)
    await turns.run_turn(*key)
    await db.refresh(session)
    assert session.stop_reason == {"type": "end_turn"}
    assert (await db.get(SessionTurn, key)).done


async def test_heartbeat_failure_cancels_running_work(
    db, session, dispatch, monkeypatch
):
    key = await accepted(db, session)
    cancelled = asyncio.Event()

    async def slow(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(sessions, "process_session", lambda *args, **kwargs: slow())
    monkeypatch.setattr(
        turns.Execution, "heartbeat", AsyncMock(side_effect=ConnectionError("lost DB"))
    )
    await turns.run_turn(*key)
    assert cancelled.is_set()
    turn = await db.get(SessionTurn, key)
    assert not turn.done and turn.owner is None
    assert await turns.acquire(*key) is None  # retry delay cannot be bypassed


@pytest.mark.parametrize("failure", [False, True])
async def test_ack_only_after_durable_processing(monkeypatch, failure):
    message = Mock(
        data=json.dumps({"session_id": "sess_test", "generation": 1}).encode()
    )

    async def run(*args):
        message.ack.assert_not_called()
        if failure:
            raise ConnectionError("DB unavailable")

    monkeypatch.setattr(turns, "run_turn", run)
    if failure:
        with pytest.raises(ConnectionError):
            await pubsub.handle_message(message)
        message.nack.assert_called_once()
        message.ack.assert_not_called()
    else:
        await pubsub.handle_message(message)
        message.ack.assert_called_once()


@pytest.mark.parametrize(
    "payload", [b"not json", b"[]", b'{"session_id":"x","generation":true}']
)
async def test_invalid_messages_do_not_loop_forever(payload, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(turns, "run_turn", run)
    message = Mock(data=payload)
    await pubsub.handle_message(message)
    run.assert_not_called()
    message.ack.assert_called_once()


@pytest.fixture
def graph_runtime(tmp_path, monkeypatch):
    checkpoint_file = str(tmp_path / "checkpoints.sqlite")

    @asynccontextmanager
    async def saver(_session_id):
        async with AsyncSqliteSaver.from_conn_string(checkpoint_file) as checkpoint:
            yield checkpoint

    monkeypatch.setattr(engine, "_checkpoint_saver", saver)
    monkeypatch.setattr(engine, "_build_chat_model", lambda *args, **kwargs: object())

    async def run(execution, builder):
        monkeypatch.setattr(
            engine,
            "create_deep_agent",
            lambda **kwargs: builder.compile(checkpointer=kwargs["checkpointer"]),
        )

        async def emit(kind, payload):
            return await execution.emit(kind, kind, payload)

        return await engine.execute_agent(
            session=SimpleNamespace(id=execution.session_id, model=None),
            version=SimpleNamespace(
                agent_id="agent",
                version=1,
                tools=[{"type": engine.AGENT_TOOLSET}],
                skills=[],
                model={},
                system=None,
            ),
            events=execution.events,
            sandbox=SimpleNamespace(to_deep_agent_backend=object()),
            emit=emit,
            publish=AsyncMock(),
            inference_key="test",
            recovery=Recovery(execution),
        )

    return run, saver


async def test_restart_resumes_checkpoint_without_repeating_input_or_output(
    db,
    session,
    dispatch,
    graph_runtime,
):
    run, saver = graph_runtime
    calls = {"model": 0, "later": 0}

    async def model(state):
        calls["model"] += 1
        return {"messages": [AIMessage(content="answer", id="answer-1")]}

    async def later(state):
        calls["later"] += 1
        if calls["later"] == 1:
            raise httpx.ReadError("process lost its connection")
        return {}

    builder = StateGraph(MessagesState)
    builder.add_node("model", model)
    builder.add_node("later", later)
    builder.add_edge(START, "model")
    builder.add_edge("model", "later")
    builder.add_edge("later", END)
    key = await accepted(db, session)
    first = await turns.acquire(*key)
    with pytest.raises(httpx.ReadError):
        await run(first, builder)
    await turns._record_failure(first, httpx.ReadError("lost"))
    await retry_ready(db)
    second = await turns.acquire(*key)
    result = await run(second, builder)
    await second.finish(result)
    assert calls == {"model": 1, "later": 2}
    events = (
        await db.scalars(
            select(SessionEvent).where(SessionEvent.type == "agent.message")
        )
    ).all()
    assert len(events) == 1
    async with saver(session.id) as checkpoint:
        graph = builder.compile(checkpointer=checkpoint)
        state = await graph.aget_state({"configurable": {"thread_id": session.id}})
        assert (
            sum(isinstance(msg, HumanMessage) for msg in state.values["messages"]) == 1
        )


async def test_completed_checkpoint_repairs_missing_public_output(
    db,
    session,
    dispatch,
    graph_runtime,
):
    run, _ = graph_runtime
    model = Mock(
        return_value={"messages": [AIMessage(content="answer", id="answer-1")]}
    )
    builder = StateGraph(MessagesState)
    builder.add_node("model", model)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    key = await accepted(db, session)
    first = await turns.acquire(*key)
    first.emit = AsyncMock(side_effect=lambda key, kind, payload: None)
    await run(first, builder)
    # Crash after the checkpoint commit but before publishing/finalizing.
    await turns._record_failure(first, httpx.ReadError("lost"))
    await retry_ready(db)
    second = await turns.acquire(*key)
    await second.finish(await run(second, builder))
    assert model.call_count == 1
    events = (
        await db.scalars(
            select(SessionEvent).where(SessionEvent.type == "agent.message")
        )
    ).all()
    assert len(events) == 1


def test_uncertain_side_effect_is_not_automatically_repeated():
    state = SimpleNamespace(
        next=("tools",),
        interrupts=(),
        values={
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call", "name": "execute", "args": {}}],
                ),
            ]
        },
    )
    with pytest.raises(RecoveryRequired):
        Recovery(None).check_resume(state)


@pytest.mark.parametrize("status, expected", [(400, False), (429, True), (503, True)])
def test_http_failures_distinguish_retryable_statuses(status, expected):
    request = httpx.Request("POST", "https://provider.example/test")
    error = httpx.HTTPStatusError(
        "provider failed",
        request=request,
        response=httpx.Response(status, request=request),
    )
    assert turns.retryable(error) is expected


async def test_restart_preserves_a_human_pause(
    db, session, dispatch, graph_runtime, monkeypatch
):
    run, _ = graph_runtime
    monkeypatch.setattr(engine, "resolve_tool_interrupts", lambda _: {"execute": True})
    calls = []

    def model(state):
        return {
            "messages": [
                AIMessage(
                    content="",
                    id="proposal",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "execute",
                            "args": {"command": "test"},
                        },
                    ],
                )
            ]
        }

    def tools(state):
        calls.append("paused")
        decision = interrupt("approval required")
        assert decision == {"decisions": [{"type": "approve"}]}
        calls.append("executed")
        return {
            "messages": [
                ToolMessage(
                    content="ok", id="result-1", name="execute", tool_call_id="call-1"
                )
            ]
        }

    builder = StateGraph(MessagesState)
    builder.add_node("model", model)
    builder.add_node("tools", tools)
    builder.add_edge(START, "model")
    builder.add_edge("model", "tools")
    builder.add_edge("tools", END)
    key = await accepted(db, session)
    first = await turns.acquire(*key)
    reason = await run(first, builder)
    assert reason == {"type": "requires_action", "tool_use_ids": ["call-1"]}
    await turns._record_failure(first, httpx.ReadError("lost before completion"))
    await retry_ready(db)
    second = await turns.acquire(*key)
    assert await run(second, builder) == reason
    await second.finish(reason)
    assert calls == ["paused"]

    # A later, explicitly approved turn must still resume the same graph.
    await sessions.send_events(
        db,
        session_id=session.id,
        organization_id=session.organization_id,
        events=[
            {
                "type": engine.event_types.USER_TOOL_CONFIRMATION,
                "tool_use_id": "call-1",
                "result": "allow",
            }
        ],
    )
    approved = await turns.acquire(session.id, session.lock_version)
    assert approved.generation == key[1] + 1
    result = await run(approved, builder)
    assert result == {"type": "end_turn"}
    await approved.finish(result)
    assert calls == ["paused", "paused", "executed"]
    rows = (
        await db.scalars(
            select(SessionEvent).where(SessionEvent.type == "agent.tool_result")
        )
    ).all()
    assert len(rows) == 1


class FakePool:
    """Cloud Run accepts updates asynchronously and enforces etag preconditions."""

    def __init__(self):
        self.pool = {
            "scaling": {"manualInstanceCount": 0}, "etag": "v1",
            "generation": "1", "observedGeneration": "1",
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
            "annotations": {"other.example/note": "preserve"},
        }
        self.patches = []

    def apply(self, body):
        if body["etag"] != self.pool["etag"]:
            raise RuntimeError("etag conflict")
        generation = str(int(self.pool["generation"]) + 1)
        self.pool.update(copy.deepcopy(body))
        self.pool.update(etag="v" + generation, generation=generation, reconciling=True)
        return {"name": "operations/" + generation}

    async def request(self, method, body=None):
        if method == "GET":
            return copy.deepcopy(self.pool)
        self.patches.append(copy.deepcopy(body))
        return self.apply(body)

    def settle(self):
        self.pool.update(reconciling=False, observedGeneration=self.pool["generation"])
        self.pool["terminalCondition"] = {"state": "CONDITION_SUCCEEDED"}


@pytest_asyncio.fixture
async def scaler(db, dispatch, monkeypatch):
    monkeypatch.setenv("VMA_WORKER_POOL_ON_DEMAND", "true")
    monkeypatch.setenv("VMA_WORKER_POOL", "projects/test/locations/us-east4/workerPools/test")
    get_settings.cache_clear()

    @asynccontextmanager
    async def scope():
        async with dispatch() as connection:
            yield connection

    monkeypatch.setattr(worker_pool, "session_scope", scope)
    fake = FakePool()
    monkeypatch.setattr(worker_pool, "_request", fake.request)
    db.add(WorkerPoolControl(id=1))  # Production's migration seeds this row.
    await db.commit()
    return fake


async def start_pool(scaler):
    assert await worker_pool.reconcile() == "pending"
    scaler.settle()
    assert await worker_pool.reconcile() == "ready"


async def age_idle(db):
    await db.execute(update(WorkerPoolControl).values(
        idle_since=turns.now() - timedelta(seconds=901)
    ))
    await db.commit()


async def test_zero_workers_recover_even_when_wake_and_publish_fail(
    db, session, scaler, monkeypatch
):
    monkeypatch.setattr(sessions, "_dispatch_turn", REAL_DISPATCH)
    monkeypatch.setattr(worker_pool, "_request", AsyncMock(side_effect=TimeoutError))
    publisher = AsyncMock(side_effect=ConnectionError)
    monkeypatch.setattr(pubsub, "publish_turn", publisher)
    key = await accepted(db, session)
    assert await turns.acquire(*key) is None
    monkeypatch.setattr(worker_pool, "_request", scaler.request)
    # Scheduler has the DB record even though neither queue nor worker can help.
    await start_pool(scaler)
    publisher.side_effect = None
    assert await turns.recover_once() == 1
    assert await turns.acquire(*key) is not None
    assert scaler.pool["scaling"]["manualInstanceCount"] == 1


@pytest.mark.parametrize("retry_wait", [False, True])
async def test_long_running_and_delayed_retry_turns_prevent_scale_down(
    db, session, scaler, retry_wait
):
    key = await accepted(db, session)
    await start_pool(scaler)
    if retry_wait:
        await db.execute(update(SessionTurn).values(retry_after=turns.now() + timedelta(hours=2)))
        await db.commit()
    else:
        assert await turns.acquire(*key) is not None
    await age_idle(db)
    assert await worker_pool.reconcile() == "ready"
    assert [p["scaling"]["manualInstanceCount"] for p in scaler.patches] == [1]


async def test_cooldown_then_stop_and_gate_waits_for_completed_restart(db, session, scaler):
    key = await accepted(db, session)
    await start_pool(scaler)
    owner = await turns.acquire(*key)
    await owner.finish({"type": "end_turn"})
    assert await worker_pool.reconcile() == "ready"
    assert len(scaler.patches) == 1  # First idle observation starts 15-minute timer.
    await age_idle(db)
    assert await worker_pool.reconcile() == "pending"
    new_key = await accepted(db, session)
    assert await turns.acquire(*new_key) is None
    assert await worker_pool.reconcile(wake_only=True) == "pending"
    assert await turns.acquire(*new_key) is None  # PATCH(1) alone is not readiness.
    scaler.settle()
    assert await worker_pool.reconcile(wake_only=True) == "ready"
    assert (await turns.acquire(*new_key)).attempts == 1
    assert [p["scaling"]["manualInstanceCount"] for p in scaler.patches] == [1, 0, 1]
    assert scaler.pool["annotations"]["other.example/note"] == "preserve"


async def test_subminute_turn_resets_idle_timer_between_scheduler_ticks(db, session, scaler):
    key = await accepted(db, session)
    await start_pool(scaler)
    await age_idle(db)
    owner = await turns.acquire(*key)
    await owner.finish({"type": "end_turn"})
    assert await worker_pool.reconcile() == "ready"
    assert len(scaler.patches) == 1


async def test_lost_stop_response_cannot_stop_a_newly_reopened_worker(
    db, session, scaler, monkeypatch
):
    key = await accepted(db, session)
    await start_pool(scaler)
    await (await turns.acquire(*key)).finish({"type": "end_turn"})
    await age_idle(db)
    delayed = []

    async def lose_request(method, body=None):
        if method == "PATCH":
            delayed.append(copy.deepcopy(body))
            raise TimeoutError("request is still in flight")
        return await scaler.request(method, body)

    monkeypatch.setattr(worker_pool, "_request", lose_request)
    with pytest.raises(TimeoutError):
        await worker_pool.reconcile()
    new_key = await accepted(db, session)
    assert await turns.acquire(*new_key) is None
    monkeypatch.setattr(worker_pool, "_request", scaler.request)
    await start_pool(scaler)
    # Count was already one, but the new annotation forces a resource change.
    # Without that fence, a delayed stop could kill this newly admitted turn.
    assert await turns.acquire(*new_key) is not None
    with pytest.raises(RuntimeError, match="etag conflict"):
        scaler.apply(delayed[0])


async def test_failed_cloud_operation_keeps_gate_closed_and_can_retry(db, session, scaler):
    key = await accepted(db, session)
    assert await worker_pool.reconcile() == "pending"
    scaler.settle()
    scaler.pool["terminalCondition"]["state"] = "CONDITION_FAILED"
    assert await worker_pool.reconcile() == "pending"
    assert await turns.acquire(*key) is None
    await start_pool(scaler)
    assert await turns.acquire(*key) is not None


@pytest.mark.postgres
async def test_new_submission_during_scale_down_cannot_start(db, session, scaler, monkeypatch):
    if db.bind.dialect.name != "postgresql":
        pytest.skip("set VMA_TEST_POSTGRES_URL for real row-lock contention")
    key = await accepted(db, session)
    await start_pool(scaler)
    await (await turns.acquire(*key)).finish({"type": "end_turn"})
    await age_idle(db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop_in_flight(method, body=None):
        if method == "PATCH":
            entered.set()
            await release.wait()
        return await scaler.request(method, body)

    monkeypatch.setattr(worker_pool, "_request", stop_in_flight)
    stopping = asyncio.create_task(worker_pool.reconcile())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        new_key = await accepted(db, session)
        acquiring = asyncio.create_task(turns.acquire(*new_key))
        # A second controller yields instead of waiting with another connection.
        assert await worker_pool.reconcile() == "busy"
    finally:
        release.set()
    assert await asyncio.wait_for(stopping, 2) == "pending"
    assert await asyncio.wait_for(acquiring, 2) is None

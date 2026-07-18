from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from deepagents.backends import StateBackend
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.engine import get_engine, reset_engine_for_tests, session_scope
from app.db.models import (
    Base,
    ManagedSession,
    Organization,
    SessionEvent,
    SessionEventIdempotency,
    UsageLedgerEntry,
)
from app.db.queries import api_keys as api_keys_q
from app.db.queries import resources as res_q
from app.organization import (
    CurrentOrganization,
    reset_current_organization,
    set_current_organization,
)
from app.runtime.checkpoints import checkpoint_saver, close_checkpoint_saver
from app.runtime.providers import RuntimeProviderCapabilities, RuntimeProviderConfig
from app.runtime.sandbox import BackendHandle, SandboxRuntimePlan
from app.runtime.work_queue import execute_work_item, lease_next_work_for_worker
from tests.conftest import TEST_API_KEY, TEST_HEADERS, TEST_ORGANIZATION_ID
from tests.test_deepagents_engine import _ScriptedModel


POSTGRES_URL = os.environ.get("VMA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="VMA_TEST_POSTGRES_URL is not configured"),
]


class SimulatedWorkerCrash(BaseException):
    """Represent process loss that bypasses normal Exception recovery."""


@pytest.fixture(autouse=True)
async def test_database(monkeypatch):
    """Use one real PostgreSQL database for both VMA and LangGraph state."""

    if not POSTGRES_URL:
        yield
        return

    database_name = make_url(POSTGRES_URL).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("VMA_TEST_POSTGRES_URL must target a database ending in _test")

    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("VMA_CHECKPOINT_DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "state")
    monkeypatch.setenv("VMA_DEFAULT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv(
        "VMA_MODEL_PROVIDERS",
        '{"fake":{"adapter":"fake","default_model":"test-model"}}',
    )
    monkeypatch.setenv("VMA_GOVERNANCE_ENABLED", "true")
    monkeypatch.setenv("VMA_WORK_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("VMA_REQUIRE_BETA_HEADER", "true")
    monkeypatch.setenv("VMA_REQUIRE_ANTHROPIC_VERSION_HEADER", "true")

    organization_token = set_current_organization(
        CurrentOrganization(
            id=TEST_ORGANIZATION_ID,
            slug="test",
            source="postgres_stage_a_test",
        )
    )
    get_settings.cache_clear()
    await close_checkpoint_saver()
    await reset_engine_for_tests()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as db:
        db.add(
            Organization(
                id=TEST_ORGANIZATION_ID,
                slug="test",
                name="PostgreSQL Stage A recovery test",
                metadata_={"provisioned_by": "test_fixture"},
            )
        )
        await api_keys_q.create_api_key(
            db,
            organization_id=TEST_ORGANIZATION_ID,
            name="PostgreSQL Stage A integration key",
            token=TEST_API_KEY,
            scopes=(
                api_keys_q.API_SCOPE,
                api_keys_q.API_KEYS_MANAGE_SCOPE,
                api_keys_q.WORKER_SCOPE,
            ),
            created_by="test_fixture",
        )
        await db.commit()

    yield

    await close_checkpoint_saver()
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


def _patch_runtime(monkeypatch, model: _ScriptedModel) -> None:
    import app.runtime.deepagents_engine as runtime_engine

    provider = RuntimeProviderConfig(
        provider="fake",
        model_id="fake-model",
        adapter="fake",
        api_key=None,
        base_url=None,
        capabilities=RuntimeProviderCapabilities(tool_calls=True),
    )
    monkeypatch.setattr(
        runtime_engine,
        "resolve_runtime_provider",
        lambda *args, **kwargs: provider,
    )
    monkeypatch.setattr(runtime_engine, "build_chat_model", lambda _provider: model)

    @asynccontextmanager
    async def fake_backend(**_kwargs):
        plan = SandboxRuntimePlan(
            enabled=True,
            backend="langgraph_state",
            supports_execute=False,
            policy_enforced=False,
            summary={"enabled": True, "backend": "langgraph_state"},
        )
        yield BackendHandle(backend=StateBackend(), plan=plan)

    monkeypatch.setattr(runtime_engine, "open_backend", fake_backend)


async def _create_queued_turn(client: AsyncClient) -> tuple[str, str, str, str]:
    suffix = uuid4().hex
    agent = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": f"Stage A Agent {suffix}", "model": {"id": "fake-model"}},
    )
    assert agent.status_code == 201, agent.text

    environment = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={
            "name": f"stage-a-worker-{suffix}",
            "config": {"type": "self_hosted"},
        },
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
    session_id = session.json()["id"]
    runtime_thread_id = f"thread_pg_stage_a_{uuid4().hex}"
    async with session_scope() as db:
        managed_session = await db.get(ManagedSession, session_id)
        assert managed_session is not None
        managed_session.runtime_thread_id = runtime_thread_id
        await db.commit()

    idempotency_key = f"stage-a-turn-{uuid4().hex}"
    submitted = await client.post(
        f"/v1/sessions/{session_id}/events",
        headers={**TEST_HEADERS, "Idempotency-Key": idempotency_key},
        json={"events": [{"type": "user.message", "content": "recover exactly once"}]},
    )
    assert submitted.status_code == 200, submitted.text

    async with session_scope() as db:
        resources = await res_q.list_resources(
            db,
            resource_type="environment_work",
            parent_id=environment.json()["id"],
            limit=10,
            organization_id=TEST_ORGANIZATION_ID,
        )
    assert len(resources) == 1
    work_id = resources[0].id
    assert work_id
    return session_id, environment.json()["id"], work_id, runtime_thread_id


async def _lease_work(environment_id: str, *, worker_id: str) -> tuple[str, str, int]:
    async with session_scope() as db:
        work = await lease_next_work_for_worker(
            db,
            environment_id=environment_id,
            worker_id=worker_id,
            lease_seconds=60,
        )
        assert work is not None
        lease = dict((work.data or {}).get("lease") or {})
        await db.commit()
    return str(lease["lease_id"]), worker_id, int(lease["generation"])


async def _expire_work_lease(work_id: str) -> None:
    async with session_scope() as db:
        work = await res_q.get_work_item_for_worker(db, work_id)
        assert work is not None
        data = dict(work.data or {})
        lease = dict(data.get("lease") or {})
        lease["expires_at"] = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        data["lease"] = lease
        await res_q.update_resource(db, work, data=data, status="running")
        await db.commit()


async def _execute_leased_work(
    work_id: str,
    lease: tuple[str, str, int],
) -> str:
    lease_id, worker_id, generation = lease
    return await execute_work_item(
        work_id,
        worker_id=worker_id,
        lease_id=lease_id,
        lease_generation=generation,
        lease_seconds=60,
    )


async def _retry_after_lease_takeover(environment_id: str, work_id: str) -> str:
    await _expire_work_lease(work_id)
    lease = await _lease_work(environment_id, worker_id=f"worker-retry-{uuid4().hex}")
    return await _execute_leased_work(work_id, lease)


async def _assert_exactly_once_rows(session_id: str, work_id: str, *, total_tokens: int) -> None:
    async with session_scope() as db:
        agent_message_count = await db.scalar(
            select(func.count())
            .select_from(SessionEvent)
            .where(
                SessionEvent.session_id == session_id,
                SessionEvent.type == "agent.message",
            )
        )
        usage_rows = list(
            (
                await db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == TEST_ORGANIZATION_ID,
                        UsageLedgerEntry.metric == "model_tokens",
                        UsageLedgerEntry.source_id == session_id,
                    )
                )
            ).scalars()
        )
        submission_count = await db.scalar(
            select(func.count())
            .select_from(SessionEventIdempotency)
            .where(SessionEventIdempotency.session_id == session_id)
        )

    assert agent_message_count == 1
    assert len(usage_rows) == 1
    assert usage_rows[0].quantity == total_tokens
    assert usage_rows[0].idempotency_key == f"model_tokens:{work_id}"
    assert submission_count == 1


async def _usage_row_count(session_id: str) -> int:
    async with session_scope() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(UsageLedgerEntry)
                .where(
                    UsageLedgerEntry.organization_id == TEST_ORGANIZATION_ID,
                    UsageLedgerEntry.metric == "model_tokens",
                    UsageLedgerEntry.source_id == session_id,
                )
            )
            or 0
        )


async def _assert_postgres_checkpoint_phase(
    runtime_thread_id: str,
    *,
    expected_phase: str,
) -> None:
    async with checkpoint_saver() as saver:
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": runtime_thread_id}}
        )
    assert checkpoint is not None
    values = checkpoint.checkpoint.get("channel_values")
    assert isinstance(values, dict)
    marker = values.get("vma_turn_marker")
    assert isinstance(marker, dict)
    assert marker.get("phase") == expected_phase


async def test_completed_marker_crash_before_journal_recovers_without_model_replay(
    postgres_client,
    monkeypatch,
):
    import app.runtime.runner as runner

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="completed checkpoint answer",
                usage_metadata={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
            )
        ]
    )
    _patch_runtime(monkeypatch, model)
    session_id, environment_id, work_id, runtime_thread_id = await _create_queued_turn(
        postgres_client
    )
    original_persist_turn_journal = runner._persist_turn_journal
    journal_calls = 0

    async def crash_once_before_journal(*args, **kwargs):
        nonlocal journal_calls
        journal_calls += 1
        if journal_calls == 1:
            raise SimulatedWorkerCrash("crash after completed marker")
        return await original_persist_turn_journal(*args, **kwargs)

    monkeypatch.setattr(runner, "_persist_turn_journal", crash_once_before_journal)

    first_lease = await _lease_work(environment_id, worker_id=f"worker-a-{uuid4().hex}")
    with pytest.raises(SimulatedWorkerCrash, match="completed marker"):
        await _execute_leased_work(work_id, first_lease)

    assert model._index == 1
    await _assert_postgres_checkpoint_phase(runtime_thread_id, expected_phase="completed")
    await _expire_work_lease(work_id)
    second_lease = await _lease_work(environment_id, worker_id=f"worker-b-{uuid4().hex}")

    assert await _execute_leased_work(work_id, second_lease) == "completed"
    assert model._index == 1
    assert journal_calls == 2
    await _assert_exactly_once_rows(session_id, work_id, total_tokens=7)


async def test_final_model_node_crash_before_completion_marker_resumes_without_model_replay(
    postgres_client,
    monkeypatch,
):
    import app.runtime.deepagents_engine as runtime_engine

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="final node checkpoint answer",
                usage_metadata={"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
            )
        ]
    )
    _patch_runtime(monkeypatch, model)
    session_id, environment_id, work_id, runtime_thread_id = await _create_queued_turn(
        postgres_client
    )
    original_after_agent = runtime_engine.VmaTurnCompletionMiddleware.aafter_agent
    completion_calls = 0

    async def crash_once_before_completion_marker(self, state, runtime):
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise SimulatedWorkerCrash("crash before completion marker")
        return await original_after_agent(self, state, runtime)

    monkeypatch.setattr(
        runtime_engine.VmaTurnCompletionMiddleware,
        "aafter_agent",
        crash_once_before_completion_marker,
    )

    first_lease = await _lease_work(environment_id, worker_id=f"worker-a-{uuid4().hex}")
    with pytest.raises(SimulatedWorkerCrash, match="completion marker"):
        await _execute_leased_work(work_id, first_lease)

    assert model._index == 1
    assert completion_calls == 1
    await _assert_postgres_checkpoint_phase(runtime_thread_id, expected_phase="started")
    await _expire_work_lease(work_id)
    second_lease = await _lease_work(environment_id, worker_id=f"worker-b-{uuid4().hex}")

    assert await _execute_leased_work(work_id, second_lease) == "completed"
    assert model._index == 1
    assert completion_calls == 2
    await _assert_exactly_once_rows(session_id, work_id, total_tokens=9)


@pytest.mark.parametrize(
    "crash_after_usage",
    [False, True],
    ids=["post-journal-pre-usage", "post-usage-pre-finalize"],
)
async def test_journal_and_usage_boundaries_recover_exactly_once(
    postgres_client,
    monkeypatch,
    crash_after_usage,
):
    import app.runtime.runner as runner

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="journal boundary answer",
                usage_metadata={"input_tokens": 6, "output_tokens": 5, "total_tokens": 11},
            )
        ]
    )
    _patch_runtime(monkeypatch, model)
    session_id, environment_id, work_id, _runtime_thread_id = await _create_queued_turn(
        postgres_client
    )
    original_record_usage = runner._record_model_usage_after_result
    usage_calls = 0

    async def crash_once_at_usage_boundary(*args, **kwargs):
        nonlocal usage_calls
        usage_calls += 1
        if usage_calls == 1 and not crash_after_usage:
            raise SimulatedWorkerCrash("crash after journal")
        result = await original_record_usage(*args, **kwargs)
        if usage_calls == 1:
            raise SimulatedWorkerCrash("crash after usage")
        return result

    monkeypatch.setattr(
        runner,
        "_record_model_usage_after_result",
        crash_once_at_usage_boundary,
    )

    first_lease = await _lease_work(environment_id, worker_id=f"worker-a-{uuid4().hex}")
    expected_message = "crash after usage" if crash_after_usage else "crash after journal"
    with pytest.raises(SimulatedWorkerCrash, match=expected_message):
        await _execute_leased_work(work_id, first_lease)

    assert model._index == 1
    assert await _usage_row_count(session_id) == int(crash_after_usage)
    assert await _retry_after_lease_takeover(environment_id, work_id) == "completed"
    assert model._index == 1
    assert usage_calls == 2
    await _assert_exactly_once_rows(session_id, work_id, total_tokens=11)


async def test_pre_finalize_transaction_crash_recovers_exactly_once(
    postgres_client,
    monkeypatch,
):
    import app.runtime.runner as runner

    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="pre-finalize answer",
                usage_metadata={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
            )
        ]
    )
    _patch_runtime(monkeypatch, model)
    session_id, environment_id, work_id, _runtime_thread_id = await _create_queued_turn(
        postgres_client
    )
    original_update_session = runner.sessions_q.update_session
    finalize_calls = 0

    async def crash_once_before_idle_commit(db, session, **kwargs):
        nonlocal finalize_calls
        if kwargs.get("status") == "idle":
            finalize_calls += 1
            if finalize_calls == 1:
                raise SimulatedWorkerCrash("crash before finalize commit")
        return await original_update_session(db, session, **kwargs)

    monkeypatch.setattr(runner.sessions_q, "update_session", crash_once_before_idle_commit)

    first_lease = await _lease_work(environment_id, worker_id=f"worker-a-{uuid4().hex}")
    with pytest.raises(SimulatedWorkerCrash, match="before finalize commit"):
        await _execute_leased_work(work_id, first_lease)

    assert model._index == 1
    assert await _usage_row_count(session_id) == 1
    assert await _retry_after_lease_takeover(environment_id, work_id) == "completed"
    assert model._index == 1
    assert finalize_calls == 2
    await _assert_exactly_once_rows(session_id, work_id, total_tokens=12)

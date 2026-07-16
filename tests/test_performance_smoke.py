import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scripts.performance_smoke import (
    SmokeConfig,
    _TurnObservation,
    cleanup_owned_resources,
    run_smoke,
    OwnedResources,
)


class _AsyncItems:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        async def iterate():
            for item in self._items:
                yield item

        return iterate()


class _FakeVaults:
    def __init__(self):
        self.retrieved = []

    async def retrieve(self, vault_id):
        self.retrieved.append(vault_id)
        return SimpleNamespace(id=vault_id)


class _FakeAgents:
    def __init__(self):
        self.created = []
        self.retrieved = []
        self.archived = []

    async def create(self, **body):
        self.created.append(body)
        return SimpleNamespace(id="agent_smoke")

    async def retrieve(self, agent_id):
        self.retrieved.append(agent_id)
        return SimpleNamespace(id=agent_id)

    async def archive(self, agent_id):
        self.archived.append(agent_id)
        return SimpleNamespace(id=agent_id)


class _FakeEnvironments:
    def __init__(self):
        self.created = []
        self.retrieved = []
        self.deleted = []

    async def create(self, **body):
        self.created.append(body)
        return SimpleNamespace(id="environment_smoke")

    async def retrieve(self, environment_id):
        self.retrieved.append(environment_id)
        return SimpleNamespace(id=environment_id)

    async def delete(self, environment_id):
        self.deleted.append(environment_id)
        return SimpleNamespace(id=environment_id, deleted=True)


class _FakeEvents:
    def __init__(self):
        self.sent = {}
        self.in_flight = 0
        self.max_in_flight = 0

    async def send(self, session_id, *, events, idempotency_key):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        accepted_at = datetime.now(timezone.utc)
        self.sent[session_id] = {
            "accepted_at": accepted_at,
            "text": events[0]["content"][0]["text"],
            "idempotency_key": idempotency_key,
        }
        self.in_flight -= 1
        return SimpleNamespace(
            data=[SimpleNamespace(seq=2, created_at=accepted_at)]
        )

    def list(self, session_id, *, after_seq, limit, order):
        sent = self.sent[session_id]
        accepted_at = sent["accepted_at"]
        return _AsyncItems(
            [
                {
                    "id": f"evt-{session_id}-running",
                    "seq": after_seq + 1,
                    "type": "session.status_running",
                    "created_at": accepted_at + timedelta(milliseconds=100),
                },
                {
                    "id": f"evt-{session_id}-message",
                    "seq": after_seq + 2,
                    "type": "agent.message",
                    "content": [{"type": "text", "text": sent["text"]}],
                    "created_at": accepted_at + timedelta(milliseconds=250),
                },
                {
                    "id": f"evt-{session_id}-idle",
                    "seq": after_seq + 3,
                    "type": "session.status_idle",
                    "stop_reason": {"type": "end_turn"},
                    "created_at": accepted_at + timedelta(milliseconds=400),
                },
            ]
        )


class _FakeSessions:
    def __init__(self):
        self.events = _FakeEvents()
        self.created = []
        self.deleted = []
        self.cancelled = []

    async def create(self, **body):
        session_id = f"session_{len(self.created) + 1}"
        self.created.append({"id": session_id, **body})
        return SimpleNamespace(id=session_id)

    async def delete(self, session_id):
        self.deleted.append(session_id)
        return SimpleNamespace(id=session_id, deleted=True)

    async def cancel(self, session_id):
        self.cancelled.append(session_id)
        return SimpleNamespace(id=session_id, status="terminated")


class _FakeClient:
    def __init__(self):
        self.vaults = _FakeVaults()
        self.agents = _FakeAgents()
        self.environments = _FakeEnvironments()
        self.sessions = _FakeSessions()


def test_turn_observation_uses_server_timestamps_and_tracks_nonce():
    accepted_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    observation = _TurnObservation(accepted_at=accepted_at, trigger_started=100.0)
    observation.observe(
        {
            "type": "session.status_running",
            "created_at": accepted_at + timedelta(milliseconds=120),
        },
        nonce="nonce-1",
        now=101.0,
    )
    observation.observe(
        {
            "type": "agent.message",
            "content": [{"type": "text", "text": "nonce-1"}],
            "created_at": accepted_at + timedelta(milliseconds=310),
        },
        nonce="nonce-1",
        now=102.0,
    )
    observation.observe(
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "end_turn"},
            "created_at": accepted_at + timedelta(milliseconds=550),
        },
        nonce="nonce-1",
        now=103.0,
    )

    assert observation.queue_wait_ms == 120
    assert observation.first_event_ms == 310
    assert observation.first_event_type == "agent.message"
    assert observation.total_ms == 550
    assert observation.stop_reason_type == "end_turn"
    assert observation.response_contains_nonce is True


async def test_smoke_runs_turns_concurrently_and_cleans_only_owned_resources():
    client = _FakeClient()
    report = await run_smoke(
        client,
        SmokeConfig(
            vault_ids=("vault_customer",),
            session_count=3,
            provision_concurrency=2,
            poll_interval=0.001,
            cleanup_timeout=1,
        ),
    )

    assert report.ok is True
    assert report.passed == 3
    assert client.sessions.events.max_in_flight == 3
    assert client.sessions.deleted == ["session_1", "session_2", "session_3"]
    assert client.agents.archived == ["agent_smoke"]
    assert client.environments.deleted == ["environment_smoke"]
    assert client.vaults.retrieved == ["vault_customer"]
    assert all(result.queue_wait_ms == 100 for result in report.results)
    assert all(result.first_event_ms == 250 for result in report.results)
    assert all(result.total_ms == 400 for result in report.results)


async def test_supplied_agent_and_environment_are_never_cleaned_up():
    client = _FakeClient()
    report = await run_smoke(
        client,
        SmokeConfig(
            vault_ids=("vault_customer",),
            session_count=1,
            agent_id="agent_existing",
            environment_id="environment_existing",
            poll_interval=0.001,
            cleanup_timeout=1,
        ),
    )

    assert report.ok is True
    assert client.agents.created == []
    assert client.environments.created == []
    assert client.agents.retrieved == ["agent_existing"]
    assert client.environments.retrieved == ["environment_existing"]
    assert client.agents.archived == []
    assert client.environments.deleted == []
    assert client.sessions.deleted == ["session_1"]


class _NoTerminalEvents(_FakeEvents):
    def __init__(self):
        super().__init__()
        self._emitted = set()

    def list(self, session_id, *, after_seq, limit, order):
        if session_id in self._emitted:
            return _AsyncItems([])
        self._emitted.add(session_id)
        sent = self.sent[session_id]
        accepted_at = sent["accepted_at"]
        return _AsyncItems(
            [
                {
                    "id": f"evt-{session_id}-running",
                    "seq": after_seq + 1,
                    "type": "session.status_running",
                    "created_at": accepted_at + timedelta(milliseconds=90),
                },
                {
                    "id": f"evt-{session_id}-message",
                    "seq": after_seq + 2,
                    "type": "agent.message",
                    "content": [{"type": "text", "text": sent["text"]}],
                    "created_at": accepted_at + timedelta(milliseconds=180),
                },
            ]
        )


async def test_timeout_report_keeps_partial_event_timings():
    client = _FakeClient()
    client.sessions.events = _NoTerminalEvents()
    report = await run_smoke(
        client,
        SmokeConfig(
            vault_ids=("vault_customer",),
            session_count=1,
            turn_timeout=0.02,
            poll_interval=0.001,
            cleanup_timeout=1,
        ),
    )

    result = report.results[0]
    assert result.success is False
    assert result.failure_stage == "turn"
    assert result.queue_wait_ms == 90
    assert result.first_event_ms == 180
    assert result.first_event_type == "agent.message"
    assert result.total_ms is None
    assert client.sessions.deleted == ["session_1"]


class _Conflict(Exception):
    status_code = 409


async def test_cleanup_cancels_running_session_before_retrying_delete():
    client = _FakeClient()
    original_delete = client.sessions.delete
    attempts = 0

    async def conflict_once(session_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _Conflict("still running")
        return await original_delete(session_id)

    client.sessions.delete = conflict_once
    report = await cleanup_owned_resources(
        client,
        OwnedResources(session_ids=["session_running"]),
        timeout=1,
        poll_interval=0.001,
        concurrency=1,
    )

    assert report.errors == []
    assert report.deleted_sessions == ["session_running"]
    assert client.sessions.cancelled == ["session_running"]

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import AlreadyExists
from structlog.testing import capture_logs

from app.config import Settings
from app.runtime import dispatch as dispatch_module
from app.runtime.dispatch import (
    CloudTasksDispatcher,
    NoopDispatcher,
    RecordingDispatcher,
    close_dispatcher,
    dispatch_work,
    get_dispatcher,
    reset_dispatcher,
    set_dispatcher,
    task_id_for_work,
    validate_dispatch_settings,
)


class FakeCloudTasksClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[dict] = []
        self.closed = False

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    async def create_task(self, *, request: dict):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return request["task"]

    async def close(self) -> None:
        self.closed = True


def _hybrid_settings(**overrides) -> Settings:
    values = {
        "vma_work_dispatch_mode": "hybrid",
        "vma_tasks_queue": "vma-turns",
        "vma_tasks_location": "us-central1",
        "vma_tasks_service_account": "vma-runtime@example.iam.gserviceaccount.com",
        "vma_worker_url": "https://worker.example.run.app",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
async def _clear_dispatcher():
    await reset_dispatcher()
    yield
    await reset_dispatcher()


def test_dispatch_config_defaults_to_poll():
    settings = Settings(vma_work_dispatch_mode="poll")

    assert settings.vma_work_dispatch_mode == "poll"
    assert settings.vma_worker_turn_limit == 5
    assert validate_dispatch_settings(settings) is settings


@pytest.mark.parametrize(
    ("field", "expected_name"),
    [
        ("vma_tasks_queue", "VMA_TASKS_QUEUE"),
        ("vma_tasks_location", "VMA_TASKS_LOCATION"),
        ("vma_tasks_service_account", "VMA_TASKS_SERVICE_ACCOUNT"),
        ("vma_worker_url", "VMA_WORKER_URL"),
    ],
)
def test_hybrid_config_rejects_each_missing_setting(field: str, expected_name: str):
    settings = _hybrid_settings(**{field: "  "})

    with pytest.raises(ValueError, match=expected_name):
        validate_dispatch_settings(settings)


def test_hybrid_config_accepts_complete_settings():
    settings = _hybrid_settings()

    assert validate_dispatch_settings(settings) is settings


def test_task_id_has_hash_prefix_and_attempt_suffix():
    assert task_id_for_work("work_123", attempt=4) == "wk-a5e151d9-work_123-a4"


@pytest.mark.asyncio
async def test_dispatcher_creates_named_oidc_http_task(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-from-env")
    client = FakeCloudTasksClient()
    dispatcher = CloudTasksDispatcher(
        queue="vma-turns",
        location="us-central1",
        service_account="vma-runtime@example.iam.gserviceaccount.com",
        worker_url="https://worker.example.run.app/",
        client=client,
    )

    await dispatcher.dispatch("work_123", attempt=2)

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["parent"] == (
        "projects/project-from-env/locations/us-central1/queues/vma-turns"
    )
    task = request["task"]
    assert task.name == (
        "projects/project-from-env/locations/us-central1/queues/vma-turns/tasks/"
        "wk-a5e151d9-work_123-a2"
    )
    assert task.http_request.url == (
        "https://worker.example.run.app/internal/work/work_123/execute"
    )
    assert task.http_request.body == b"{}"
    assert task.http_request.headers["Content-Type"] == "application/json"
    assert task.http_request.oidc_token.service_account_email == (
        "vma-runtime@example.iam.gserviceaccount.com"
    )
    assert task.http_request.oidc_token.audience == "https://worker.example.run.app"
    assert task.dispatch_deadline.seconds == 1800


@pytest.mark.asyncio
async def test_dispatcher_propagates_schedule_time_and_quotes_work_id():
    client = FakeCloudTasksClient()
    dispatcher = CloudTasksDispatcher(
        queue="vma-turns",
        location="us-central1",
        service_account="vma-runtime@example.iam.gserviceaccount.com",
        worker_url="https://worker.example.run.app",
        client=client,
        project_id="project-id",
    )
    schedule_at = datetime(2026, 7, 17, 12, 30, 45, tzinfo=UTC)

    await dispatcher.dispatch("work/with space", attempt=1, schedule_at=schedule_at)

    task = client.requests[0]["task"]
    assert task.http_request.url.endswith(
        "/internal/work/work%2Fwith%20space/execute"
    )
    assert task.schedule_time == schedule_at


@pytest.mark.asyncio
async def test_dispatcher_uses_adc_project_when_env_is_absent(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(
        dispatch_module,
        "_default_google_credentials",
        lambda: (object(), "adc-project"),
    )
    client = FakeCloudTasksClient()
    dispatcher = CloudTasksDispatcher(
        queue="vma-turns",
        location="us-central1",
        service_account="vma-runtime@example.iam.gserviceaccount.com",
        worker_url="https://worker.example.run.app",
        client=client,
    )

    await dispatcher.dispatch("work_123", attempt=0)

    assert client.requests[0]["parent"].startswith("projects/adc-project/")


@pytest.mark.asyncio
async def test_already_exists_is_swallowed():
    client = FakeCloudTasksClient(AlreadyExists("already exists"))
    dispatcher = CloudTasksDispatcher(
        queue="vma-turns",
        location="us-central1",
        service_account="vma-runtime@example.iam.gserviceaccount.com",
        worker_url="https://worker.example.run.app",
        client=client,
        project_id="project-id",
    )

    await dispatcher.dispatch("work_123", attempt=0)

    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_creation_failure_is_logged_and_swallowed():
    client = FakeCloudTasksClient(RuntimeError("queue unavailable"))
    dispatcher = CloudTasksDispatcher(
        queue="vma-turns",
        location="us-central1",
        service_account="vma-runtime@example.iam.gserviceaccount.com",
        worker_url="https://worker.example.run.app",
        client=client,
        project_id="project-id",
    )

    with capture_logs() as logs:
        await dispatcher.dispatch("work_123", attempt=0)

    assert any(log["event"] == "cloud_tasks_dispatch_failed" for log in logs)


@pytest.mark.asyncio
async def test_recording_dispatcher_and_cached_wrapper():
    recording = RecordingDispatcher()
    set_dispatcher(recording)
    schedule_at = datetime(2026, 7, 17, tzinfo=UTC)

    await dispatch_work("work_123", attempt=3, schedule_at=schedule_at)

    assert get_dispatcher() is recording
    assert recording.calls == [
        dispatch_module.DispatchCall("work_123", 3, schedule_at)
    ]


@pytest.mark.asyncio
async def test_close_dispatcher_closes_cached_client():
    client = FakeCloudTasksClient()
    dispatcher = CloudTasksDispatcher(
        queue="vma-turns",
        location="us-central1",
        service_account="vma-runtime@example.iam.gserviceaccount.com",
        worker_url="https://worker.example.run.app",
        client=client,
        project_id="project-id",
    )
    set_dispatcher(dispatcher)

    await close_dispatcher()

    assert client.closed is True


@pytest.mark.asyncio
async def test_poll_mode_builds_a_noop_dispatcher(monkeypatch):
    monkeypatch.setenv("VMA_WORK_DISPATCH_MODE", "poll")
    dispatch_module.get_settings.cache_clear()

    assert isinstance(get_dispatcher(), NoopDispatcher)

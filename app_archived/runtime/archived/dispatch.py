from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha1
from typing import Any, Protocol
from urllib.parse import quote

import structlog

from app.config import Settings, get_settings


logger = structlog.get_logger(__name__)

_TASK_DISPATCH_DEADLINE_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class DispatchCall:
    work_id: str
    attempt: int
    schedule_at: datetime | None = None


class WorkDispatcher(Protocol):
    async def dispatch(
        self,
        work_id: str,
        *,
        attempt: int,
        schedule_at: datetime | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


class NoopDispatcher:
    async def dispatch(
        self,
        work_id: str,
        *,
        attempt: int,
        schedule_at: datetime | None = None,
    ) -> None:
        return None

    async def close(self) -> None:
        return None


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[DispatchCall] = []

    async def dispatch(
        self,
        work_id: str,
        *,
        attempt: int,
        schedule_at: datetime | None = None,
    ) -> None:
        self.calls.append(
            DispatchCall(
                work_id=work_id,
                attempt=attempt,
                schedule_at=schedule_at,
            )
        )

    async def close(self) -> None:
        return None


class CloudTasksDispatcher:
    def __init__(
        self,
        *,
        queue: str,
        location: str,
        service_account: str,
        worker_url: str,
        client: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        self._queue = queue.strip()
        self._location = location.strip()
        self._service_account = service_account.strip()
        self._worker_url = worker_url.strip().rstrip("/")
        self._client = client
        self._project_id = project_id.strip() if project_id else None
        self._credentials: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> CloudTasksDispatcher:
        return cls(
            queue=settings.vma_tasks_queue,
            location=settings.vma_tasks_location,
            service_account=settings.vma_tasks_service_account,
            worker_url=settings.vma_worker_url,
        )

    async def dispatch(
        self,
        work_id: str,
        *,
        attempt: int,
        schedule_at: datetime | None = None,
    ) -> None:
        try:
            project_id = self._resolved_project_id()
            client = self._client_instance()
            parent = client.queue_path(project_id, self._location, self._queue)
            task = self._task(
                parent=parent,
                work_id=work_id,
                attempt=attempt,
                schedule_at=schedule_at,
            )
            await client.create_task(request={"parent": parent, "task": task})
        except Exception as exc:
            if _is_already_exists(exc):
                logger.debug(
                    "cloud_tasks_dispatch_already_exists",
                    work_id=work_id,
                    attempt=attempt,
                )
            else:
                logger.exception(
                    "cloud_tasks_dispatch_failed",
                    work_id=work_id,
                    attempt=attempt,
                )

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        result = client.close()
        if inspect.isawaitable(result):
            await result

    def _client_instance(self):
        if self._client is None:
            from google.cloud import tasks_v2

            kwargs = (
                {"credentials": self._credentials}
                if self._credentials is not None
                else {}
            )
            self._client = tasks_v2.CloudTasksAsyncClient(**kwargs)
        return self._client

    def _resolved_project_id(self) -> str:
        if self._project_id:
            return self._project_id
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project_id:
            credentials, discovered_project = _default_google_credentials()
            self._credentials = credentials
            project_id = str(discovered_project or "").strip()
        if not project_id:
            raise RuntimeError(
                "Cloud Tasks dispatch requires GOOGLE_CLOUD_PROJECT or an ADC project"
            )
        self._project_id = project_id
        return project_id

    def _task(
        self,
        *,
        parent: str,
        work_id: str,
        attempt: int,
        schedule_at: datetime | None,
    ) -> Any:
        from google.cloud import tasks_v2
        from google.protobuf import duration_pb2, timestamp_pb2

        task_id = task_id_for_work(work_id, attempt=attempt)
        task = tasks_v2.Task(
            name=f"{parent}/tasks/{task_id}",
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=(
                    f"{self._worker_url}/internal/work/"
                    f"{quote(work_id, safe='')}/execute"
                ),
                headers={"Content-Type": "application/json"},
                body=b"{}",
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._service_account,
                    audience=self._worker_url,
                ),
            ),
            dispatch_deadline=duration_pb2.Duration(
                seconds=_TASK_DISPATCH_DEADLINE_SECONDS
            ),
        )
        if schedule_at is not None:
            scheduled = schedule_at
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=UTC)
            timestamp = timestamp_pb2.Timestamp()
            timestamp.FromDatetime(scheduled)
            task.schedule_time = timestamp
        return task


def task_id_for_work(work_id: str, *, attempt: int) -> str:
    prefix = sha1(work_id.encode("utf-8")).hexdigest()[:8]
    return f"wk-{prefix}-{work_id}-a{attempt}"


def _default_google_credentials():
    import google.auth

    return google.auth.default()


def _is_already_exists(exc: Exception) -> bool:
    try:
        from google.api_core.exceptions import AlreadyExists
    except ImportError:
        return False

    return isinstance(exc, AlreadyExists)


def validate_dispatch_settings(settings: Settings | None = None) -> Settings:
    resolved = settings or get_settings()
    if resolved.vma_work_dispatch_mode == "poll":
        return resolved

    required = {
        "VMA_TASKS_QUEUE": resolved.vma_tasks_queue,
        "VMA_TASKS_LOCATION": resolved.vma_tasks_location,
        "VMA_TASKS_SERVICE_ACCOUNT": resolved.vma_tasks_service_account,
        "VMA_WORKER_URL": resolved.vma_worker_url,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise ValueError(
            "VMA_WORK_DISPATCH_MODE=hybrid requires: " + ", ".join(missing)
        )
    return resolved


_dispatcher: WorkDispatcher | None = None


def get_dispatcher() -> WorkDispatcher:
    global _dispatcher
    if _dispatcher is None:
        settings = validate_dispatch_settings()
        if settings.vma_work_dispatch_mode == "poll":
            _dispatcher = NoopDispatcher()
        else:
            _dispatcher = CloudTasksDispatcher.from_settings(settings)
    return _dispatcher


async def dispatch_work(
    work_id: str,
    *,
    attempt: int,
    schedule_at: datetime | None = None,
) -> None:
    await get_dispatcher().dispatch(
        work_id,
        attempt=attempt,
        schedule_at=schedule_at,
    )


def set_dispatcher(dispatcher: WorkDispatcher) -> None:
    global _dispatcher
    _dispatcher = dispatcher


async def close_dispatcher() -> None:
    global _dispatcher
    dispatcher = _dispatcher
    _dispatcher = None
    if dispatcher is not None:
        await dispatcher.close()


async def reset_dispatcher() -> None:
    await close_dispatcher()

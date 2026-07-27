from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from ._exceptions import APIStreamError
from ._models import SessionEvent

if TYPE_CHECKING:
    from ._client import AsyncVotrix


class SSEEvent(SessionEvent):
    """A Session event as delivered over SSE.

    Subclasses :class:`SessionEvent` so a streamed event and one read back from
    ``events.list()`` are the same shape — a reconnect that reconciles history
    against the live stream handles one type, not two. The extra fields here are
    transport-level and have no equivalent in the durable event log.
    """

    event: str | dict[str, Any] | None = None
    sse_event: str | None = None
    sse_id: str | None = None
    data: Any = None
    raw_data: str = ""
    retry: int | None = None


class AsyncEventStream:
    """An async context manager and iterator over server-sent events."""

    def __init__(
        self,
        client: "AsyncVotrix",
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_reconnects: int | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> None:
        if max_reconnects is not None and max_reconnects < 0:
            raise ValueError("max_reconnects must be non-negative")
        self._client = client
        self._method = method
        self._path = path
        self._params = params
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._max_reconnects = client.max_retries if max_reconnects is None else max_reconnects
        self._response: httpx.Response | None = None
        self._closed = False

    async def __aenter__(self) -> "AsyncEventStream":
        if self._response is not None:
            raise RuntimeError("The event stream is already open")
        if self._closed:
            raise RuntimeError("The event stream is closed")
        self._response = await self._open_response(self._initial_last_event_id())
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[SSEEvent]:
        if self._response is None:
            raise RuntimeError("Use 'async with await ...stream(...)' before iterating")
        return self._iterate()

    async def aclose(self) -> None:
        self._closed = True
        if self._response is not None:
            await self._response.aclose()
            self._response = None

    async def _iterate(self) -> AsyncIterator[SSEEvent]:
        response = self._response
        if response is None:
            raise RuntimeError("The event stream is not open")

        last_event_id = self._initial_last_event_id()
        reconnect_delay_ms: int | None = None
        reconnects = 0
        remembered_ids: deque[str] = deque()
        seen_ids: set[str] = set()

        try:
            while not self._closed:
                disconnected = False
                try:
                    async for event in _decode_sse(response):
                        if event.retry is not None and event.retry >= 0:
                            reconnect_delay_ms = event.retry
                        if event.sse_id is not None:
                            last_event_id = event.sse_id or None
                            if event.sse_id and event.sse_id in seen_ids:
                                continue
                            if event.sse_id:
                                remembered_ids.append(event.sse_id)
                                seen_ids.add(event.sse_id)
                                if len(remembered_ids) > 1024:
                                    seen_ids.discard(remembered_ids.popleft())
                        yield event
                    disconnected = True
                except (httpx.TimeoutException, httpx.TransportError):
                    disconnected = True
                finally:
                    await response.aclose()

                if not disconnected or self._closed or reconnects >= self._max_reconnects:
                    return
                await asyncio.sleep(_reconnect_delay(reconnects, reconnect_delay_ms))
                if self._closed:
                    return
                reconnects += 1
                response = await self._open_response(last_event_id)
                self._response = response
        finally:
            await response.aclose()
            self._response = None

    async def _open_response(self, last_event_id: str | None) -> httpx.Response:
        headers = httpx.Headers(self._headers)
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        elif "Last-Event-ID" in headers:
            del headers["Last-Event-ID"]
        return await self._client._open_stream(
            self._method,
            self._path,
            params=self._params,
            headers=headers,
            timeout=self._timeout,
        )

    def _initial_last_event_id(self) -> str | None:
        return httpx.Headers(self._headers).get("last-event-id")


def _reconnect_delay(attempt: int, server_retry_ms: int | None) -> float:
    if server_retry_ms is not None:
        return min(60.0, server_retry_ms / 1000)
    return min(8.0, 0.5 * (2**attempt)) * (0.75 + random.random() * 0.5)


async def _decode_sse(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    event_name: str | None = None
    event_id: str | None = None
    retry: int | None = None
    data_lines: list[str] = []

    async for line in response.aiter_lines():
        if line == "":
            if data_lines or event_name is not None or event_id is not None:
                raw_data = "\n".join(data_lines)
                yield _event_from_frame(event_name, event_id, retry, raw_data)
            event_name = None
            event_id = None
            retry = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None
        elif field == "data":
            data_lines.append(value)

    if data_lines or event_name is not None or event_id is not None:
        raw_data = "\n".join(data_lines)
        yield _event_from_frame(event_name, event_id, retry, raw_data)


def _decode_data(raw_data: str) -> Any:
    try:
        return json.loads(raw_data)
    except (TypeError, json.JSONDecodeError):
        return raw_data


def _event_from_frame(
    event_name: str | None,
    event_id: str | None,
    retry: int | None,
    raw_data: str,
) -> SSEEvent:
    decoded = _decode_data(raw_data)
    if event_name == "error" or (isinstance(decoded, dict) and decoded.get("type") == "error"):
        error = decoded.get("error") if isinstance(decoded, dict) else None
        message = error.get("message") if isinstance(error, dict) else decoded.get("message") if isinstance(decoded, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else decoded.get("type") if isinstance(decoded, dict) else None
        raise APIStreamError(
            message if isinstance(message, str) and message else "Votrix event stream failed",
            error_type=error_type if isinstance(error_type, str) else None,
            request_id=event_id,
        )
    payload = dict(decoded) if isinstance(decoded, dict) else {}
    payload_type = payload.get("type")
    payload["type"] = payload_type if isinstance(payload_type, str) else event_name or "message"
    payload.setdefault("event", event_name)
    payload.setdefault("sse_event", event_name)
    payload.setdefault("sse_id", event_id)
    payload.setdefault("data", decoded)
    payload.setdefault("raw_data", raw_data)
    payload.setdefault("retry", retry)
    return SSEEvent.model_validate(payload)

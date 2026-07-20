from __future__ import annotations

import asyncio
import json as jsonlib
import os
import random
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from ._constants import DEFAULT_BETA, SDK_VERSION
from ._exceptions import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    STATUS_ERRORS,
)
from ._models import ListEnvelope, VotrixModel

T = TypeVar("T", bound=VotrixModel)
AuthScheme = Literal["x-api-key", "bearer"]
_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504, 529}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _environment_alias(primary: str, secondary: str, *, label: str) -> str | None:
    primary_value = (os.getenv(primary) or "").strip()
    secondary_value = (os.getenv(secondary) or "").strip()
    if primary_value and secondary_value and primary_value != secondary_value:
        raise ValueError(
            f"{primary} and {secondary} are both set with different {label} values"
        )
    return primary_value or secondary_value or None


class BinaryResponse:
    """A binary HTTP response with buffered and streaming consumption modes.

    When the resource method receives ``stream=True``, ``aiter_bytes()`` and
    ``write_to_file()`` consume the network response incrementally. ``read()``
    remains available for callers that need a buffered byte string, and the
    default non-streaming mode preserves synchronous ``iter_bytes()``.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.content_type = response.headers.get("content-type")
        self.filename = _content_disposition_filename(response.headers.get("content-disposition"))
        self.status_code = response.status_code
        self.headers = response.headers

    @property
    def content(self) -> bytes:
        """Return buffered content after ``read()`` has consumed the stream."""

        try:
            return self._response.content
        except httpx.ResponseNotRead as exc:
            raise RuntimeError(
                "Binary response has not been buffered; await read(), use aiter_bytes(), "
                "or call write_to_file()"
            ) from exc

    async def __aenter__(self) -> "BinaryResponse":
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.aclose()

    async def read(self) -> bytes:
        try:
            return await self._response.aread()
        finally:
            await self._response.aclose()

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Yield downloaded bytes without buffering the full response in memory."""

        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        try:
            async for chunk in self._response.aiter_bytes(chunk_size=chunk_size):
                yield chunk
        finally:
            await self._response.aclose()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Iterate over already-buffered content for backwards compatibility."""

        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        content = self.content
        for offset in range(0, len(content), chunk_size):
            yield content[offset : offset + chunk_size]

    async def write_to_file(self, path: str | os.PathLike[str]) -> Path:
        destination = Path(path)
        try:
            content = self.content
        except RuntimeError:
            handle = await asyncio.to_thread(destination.open, "wb")
            try:
                async for chunk in self.aiter_bytes():
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
        else:
            await asyncio.to_thread(destination.write_bytes, content)
        return destination

    async def aclose(self) -> None:
        await self._response.aclose()


class AsyncVotrix:
    """Asynchronous client for the native Votrix Managed Agents API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | httpx.URL | None = None,
        auth_scheme: AuthScheme = "x-api-key",
        beta: str = DEFAULT_BETA,
        timeout: float | httpx.Timeout = 60.0,
        max_retries: int = 2,
        default_headers: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_key = (
            api_key.strip()
            if api_key is not None
            else _environment_alias(
                "VMA_API_KEY",
                "VOTRIX_VMA_API_KEY",
                label="API key",
            )
        )
        if not resolved_key:
            raise ValueError(
                "api_key is required; pass it explicitly or set VMA_API_KEY or "
                "VOTRIX_VMA_API_KEY"
            )
        resolved_base_url = (
            str(base_url).strip()
            if base_url is not None
            else (
                _environment_alias(
                    "VMA_BASE_URL",
                    "VOTRIX_VMA_BASE_URL",
                    label="base URL",
                )
                or ""
            )
        )
        if not resolved_base_url:
            raise ValueError(
                "base_url is required; pass it explicitly or set VMA_BASE_URL or "
                "VOTRIX_VMA_BASE_URL"
            )
        if auth_scheme not in {"x-api-key", "bearer"}:
            raise ValueError("auth_scheme must be 'x-api-key' or 'bearer'")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        self._api_key = resolved_key
        self.base_url = httpx.URL(resolved_base_url.rstrip("/") + "/")
        self.auth_scheme = auth_scheme
        self.max_retries = max_retries
        self.timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._default_headers = {
            "accept": "application/json",
            "user-agent": f"votrix-managed-agents-python/{SDK_VERSION}",
            "x-votrix-sdk-version": SDK_VERSION,
            "votrix-managed-agents-beta": beta,
            **dict(default_headers or {}),
        }
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=self.timeout)
        self._closed = False

        # Imported lazily to keep resource definitions independent of transport.
        from ._resources import (
            AgentsResource,
            ApiKeysResource,
            EnvironmentsResource,
            FilesResource,
            ModelProvidersResource,
            SessionsResource,
            SkillsResource,
            UsageResource,
            VaultsResource,
        )

        self.api_keys = ApiKeysResource(self)
        self.agents = AgentsResource(self)
        self.environments = EnvironmentsResource(self)
        self.sessions = SessionsResource(self)
        self.files = FilesResource(self)
        self.skills = SkillsResource(self)
        self.usage = UsageResource(self)
        self.vaults = VaultsResource(self)
        self.model_providers = ModelProvidersResource(self)

    async def __aenter__(self) -> "AsyncVotrix":
        self._ensure_open()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http_client:
            await self._http_client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        model: type[T],
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
    ) -> T:
        response = await self._request_response(
            method,
            path,
            params=params,
            json=json,
            data=data,
            files=files,
            headers=headers,
            retry=retry,
        )
        try:
            payload = response.json()
            return model.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise APIResponseValidationError(
                f"Invalid {model.__name__} response from Votrix",
                status_code=response.status_code,
                request_id=_request_id(response),
                headers=response.headers,
            ) from exc

    async def request_list(
        self,
        method: str,
        path: str,
        *,
        model: type[T],
        params: Mapping[str, Any] | None = None,
    ) -> ListEnvelope[T]:
        response = await self._request_response(method, path, params=params)
        try:
            return ListEnvelope[model].model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise APIResponseValidationError(
                f"Invalid {model.__name__} list response from Votrix",
                status_code=response.status_code,
                request_id=_request_id(response),
                headers=response.headers,
            ) from exc

    async def request_binary(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
    ) -> BinaryResponse:
        if not stream:
            response = await self._request_response(
                method,
                path,
                params=params,
                headers={"accept": "application/octet-stream"},
            )
            return BinaryResponse(response)

        self._ensure_open()
        request = self._http_client.build_request(
            method.upper(),
            self._url(path),
            params=_clean_params(params),
            headers=self._headers({"accept": "application/octet-stream"}),
            timeout=self.timeout,
        )
        response = await self._send(request, can_retry=method.upper() in _SAFE_METHODS, stream=True)
        if response.is_error:
            await response.aread()
            try:
                self._raise_status(response)
            finally:
                await response.aclose()
        return BinaryResponse(response)

    async def _request_response(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
    ) -> httpx.Response:
        self._ensure_open()
        method = method.upper()
        request = self._http_client.build_request(
            method,
            self._url(path),
            params=_clean_params(params),
            json=to_jsonable_python(json) if json is not None else None,
            data=data,
            files=files,
            headers=self._headers(headers),
            timeout=self.timeout,
        )
        is_replay_safe = method in _SAFE_METHODS or "idempotency-key" in request.headers
        can_retry = is_replay_safe and retry is not False
        response = await self._send(request, can_retry=can_retry, stream=False)
        if response.is_error:
            self._raise_status(response)
        return response

    async def _open_stream(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        self._ensure_open()
        request = self._http_client.build_request(
            method.upper(),
            self._url(path),
            params=_clean_params(params),
            headers=self._headers({"accept": "text/event-stream", **dict(headers or {})}),
            timeout=self.timeout,
        )
        response = await self._send(request, can_retry=True, stream=True)
        if response.is_error:
            await response.aread()
            try:
                self._raise_status(response)
            finally:
                await response.aclose()
        return response

    async def _send(self, request: httpx.Request, *, can_retry: bool, stream: bool) -> httpx.Response:
        attempts = self.max_retries + 1 if can_retry else 1
        replay_content: bytes | None = None
        if attempts > 1:
            try:
                replay_content = request.content
            except httpx.RequestNotRead:
                attempts = 1
        for attempt in range(attempts):
            attempt_request = request
            if attempt:
                attempt_request = httpx.Request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=replay_content,
                    extensions=dict(request.extensions),
                )
            try:
                response = await self._http_client.send(attempt_request, stream=stream)
            except httpx.TimeoutException as exc:
                if attempt + 1 >= attempts:
                    raise APITimeoutError("Request to Votrix timed out", request=attempt_request) from exc
                await asyncio.sleep(_retry_delay(attempt, None))
                continue
            except httpx.HTTPError as exc:
                if attempt + 1 >= attempts:
                    raise APIConnectionError("Could not connect to Votrix", request=attempt_request) from exc
                await asyncio.sleep(_retry_delay(attempt, None))
                continue

            if response.status_code not in _RETRYABLE_STATUSES or attempt + 1 >= attempts:
                return response
            await response.aclose()
            await asyncio.sleep(_retry_delay(attempt, response))
        raise APIConnectionError("Could not connect to Votrix", request=request)

    def _headers(self, headers: Mapping[str, str] | None) -> httpx.Headers:
        merged = httpx.Headers(self._default_headers)
        if self.auth_scheme == "bearer":
            merged["authorization"] = f"Bearer {self._api_key}"
        else:
            merged["x-api-key"] = self._api_key
        merged.update(headers or {})
        return merged

    def _url(self, path: str) -> httpx.URL:
        return self.base_url.join(path.lstrip("/"))

    def _raise_status(self, response: httpx.Response) -> None:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None
        secrets = {self._api_key, *_request_secret_values(response.request)}
        body = _redact_error_value(body, secrets)
        error = body.get("error") if isinstance(body, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(error_code, str):
            error_code = None
        message = error.get("message") if isinstance(error, dict) else None
        if (not isinstance(message, str) or not message) and isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str):
                message = detail
        if not isinstance(message, str) or not message:
            message = f"Votrix API request failed with status {response.status_code}"
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
        error_class = STATUS_ERRORS.get(response.status_code)
        if error_class is None:
            error_class = InternalServerError if response.status_code >= 500 else APIStatusError
        raise error_class(
            message,
            status_code=response.status_code,
            error_type=error_type,
            error_code=error_code,
            request_id=_request_id(response),
            body=body,
            response=response,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("AsyncVotrix client is closed")


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {key: value for key, value in params.items() if value is not None}


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    return min(60.0, max(0.0, seconds))
                except (TypeError, ValueError, OverflowError):
                    pass
    return min(8.0, 0.5 * (2**attempt)) * (0.75 + random.random() * 0.5)


def _request_id(response: httpx.Response) -> str | None:
    return response.headers.get("request-id") or response.headers.get("x-request-id")


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    return message.get_filename()


def _request_secret_values(request: httpx.Request) -> set[str]:
    try:
        payload = jsonlib.loads(request.content)
    except (TypeError, ValueError, httpx.RequestNotRead):
        return set()
    secrets: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        normalized = key.lower().replace("-", "_")
        if isinstance(value, str) and any(part in normalized for part in ("secret", "token", "api_key", "password")):
            if value:
                secrets.add(value)
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)

    visit(payload)
    return secrets


def _redact_error_value(value: Any, secrets: set[str], key: str = "") -> Any:
    normalized = key.lower().replace("-", "_")
    if isinstance(value, dict):
        return {child_key: _redact_error_value(child, secrets, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact_error_value(child, secrets, key) for child in value]
    if isinstance(value, str):
        if any(part in normalized for part in ("secret", "token", "api_key", "password")):
            return "[redacted]"
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted
    return value

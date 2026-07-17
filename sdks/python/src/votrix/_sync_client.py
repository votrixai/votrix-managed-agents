from __future__ import annotations

import json as jsonlib
import time
from collections.abc import Mapping
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from ._client import (
    AuthScheme,
    _RETRYABLE_STATUSES,
    _SAFE_METHODS,
    _clean_params,
    _environment_alias,
    _redact_error_value,
    _request_id,
    _request_secret_values,
    _retry_delay,
)
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


class Votrix:
    """Synchronous client for the native provider-BYOK API surface."""

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
        http_client: httpx.Client | None = None,
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
        self._http_client = http_client or httpx.Client(timeout=self.timeout)
        self._closed = False

        from ._sync_resources import (
            SyncApiKeysResource,
            SyncModelProvidersResource,
            SyncVaultsResource,
        )

        self.api_keys = SyncApiKeysResource(self)
        self.vaults = SyncVaultsResource(self)
        self.model_providers = SyncModelProvidersResource(self)

    def __enter__(self) -> "Votrix":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http_client:
            self._http_client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        model: type[T],
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
    ) -> T:
        response = self._request_response(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            retry=retry,
        )
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise APIResponseValidationError(
                f"Invalid {model.__name__} response from Votrix",
                status_code=response.status_code,
                request_id=_request_id(response),
                headers=response.headers,
            ) from exc

    def request_list(
        self,
        method: str,
        path: str,
        *,
        model: type[T],
        params: Mapping[str, Any] | None = None,
    ) -> ListEnvelope[T]:
        response = self._request_response(method, path, params=params)
        try:
            return ListEnvelope[model].model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise APIResponseValidationError(
                f"Invalid {model.__name__} list response from Votrix",
                status_code=response.status_code,
                request_id=_request_id(response),
                headers=response.headers,
            ) from exc

    def _request_response(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
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
            headers=self._headers(headers),
            timeout=self.timeout,
        )
        is_replay_safe = method in _SAFE_METHODS or "idempotency-key" in request.headers
        can_retry = is_replay_safe and retry is not False
        response = self._send(request, can_retry=can_retry)
        if response.is_error:
            self._raise_status(response)
        return response

    def _send(self, request: httpx.Request, *, can_retry: bool) -> httpx.Response:
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
                response = self._http_client.send(attempt_request)
            except httpx.TimeoutException as exc:
                if attempt + 1 >= attempts:
                    raise APITimeoutError("Request to Votrix timed out", request=attempt_request) from exc
                time.sleep(_retry_delay(attempt, None))
                continue
            except httpx.HTTPError as exc:
                if attempt + 1 >= attempts:
                    raise APIConnectionError("Could not connect to Votrix", request=attempt_request) from exc
                time.sleep(_retry_delay(attempt, None))
                continue
            if response.status_code not in _RETRYABLE_STATUSES or attempt + 1 >= attempts:
                return response
            response.close()
            time.sleep(_retry_delay(attempt, response))
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
        try:
            body: Any = response.json()
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
            raise RuntimeError("Votrix client is closed")

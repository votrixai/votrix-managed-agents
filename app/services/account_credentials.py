"""Provider-neutral credential values and BYOK key validation.

Platform OpenRouter provisioning deliberately stays in ``services.accounts``:
it owns and can mutate those keys through the Management API. This module is
the other trust boundary. A BYOK key is only checked, encrypted by the caller,
and handed to the matching inference backend; VMA never administers it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import SecretStr

from app.db.models.accounts import (
    CREDENTIAL_ANTHROPIC,
    CREDENTIAL_DEEPSEEK,
    CREDENTIAL_GOOGLE,
    CREDENTIAL_OPENAI,
    DIRECT_CREDENTIAL_PROVIDERS,
)
from app.models.errors import CredentialValidationUnavailable, InvalidRequest


@dataclass(frozen=True, slots=True)
class ResolvedAccountCredential:
    """The secret and routing facts needed for one Account-funded call."""

    account_id: str
    funding_mode: str
    backend: str
    api_key: SecretStr


@dataclass(frozen=True, slots=True)
class SubmittedByokCredential:
    """One direct-provider key received while creating a BYOK Account."""

    backend: str
    api_key: SecretStr


class ByokKeyValidator(Protocol):
    async def validate(self, *, backend: str, api_key: SecretStr) -> None: ...


def credential_fingerprint(*, backend: str, api_key: SecretStr) -> str:
    """A stable provider-scoped identity for duplicate detection and AES AAD.

    Scoping prevents coincidentally equal secrets issued by different systems
    from colliding. The plaintext cannot be recovered from the digest.
    """

    if backend not in DIRECT_CREDENTIAL_PROVIDERS:
        raise ValueError(f"Unsupported direct credential backend {backend!r}")
    digest = hashlib.sha256(
        f"{backend}\0{api_key.get_secret_value()}".encode("utf-8")
    ).hexdigest()
    return f"byok:{backend}:{digest}"


class HttpByokKeyValidator:
    """Validate a key with a read-only, non-inference provider endpoint."""

    _REQUESTS: dict[str, tuple[str, dict[str, str]]] = {
        CREDENTIAL_ANTHROPIC: (
            "https://api.anthropic.com/v1/models?limit=1",
            {"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
        ),
        CREDENTIAL_OPENAI: (
            "https://api.openai.com/v1/models",
            {"Authorization": "Bearer {key}"},
        ),
        CREDENTIAL_GOOGLE: (
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            {"x-goog-api-key": "{key}"},
        ),
        CREDENTIAL_DEEPSEEK: (
            "https://api.deepseek.com/models",
            {"Authorization": "Bearer {key}"},
        ),
    }

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def validate(self, *, backend: str, api_key: SecretStr) -> None:
        request = self._REQUESTS.get(backend)
        if request is None:
            raise InvalidRequest(f"Unsupported BYOK backend {backend!r}")

        url, header_template = request
        plaintext = api_key.get_secret_value().strip()
        if not plaintext:
            raise InvalidRequest("BYOK api_key cannot be empty")
        headers = {
            name: value.format(key=plaintext)
            for name, value in header_template.items()
        }

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise CredentialValidationUnavailable(
                f"{backend} could not be reached to validate the BYOK key"
            ) from exc

        if response.status_code in (401, 403):
            raise InvalidRequest(f"{backend} rejected the BYOK api_key")
        if (
            300 <= response.status_code < 400
            or response.status_code in (408, 429)
            or response.status_code >= 500
        ):
            raise CredentialValidationUnavailable(
                f"{backend} could not validate the BYOK key right now"
            )
        if response.is_error:
            # Never include the response body: providers commonly echo request
            # details, and no upstream error is allowed to disclose the key.
            raise InvalidRequest(f"{backend} rejected the BYOK api_key")

__all__ = [
    "ByokKeyValidator",
    "HttpByokKeyValidator",
    "ResolvedAccountCredential",
    "SubmittedByokCredential",
    "credential_fingerprint",
]

"""OpenRouter management-key boundary.

The credential used here is deliberately incapable of model inference.  The
runtime never imports this module; :mod:`app.runtime.engine` reaches the models
with a decrypted Account key instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Protocol

from openrouter import OpenRouter
from openrouter.utils.logger import NoOpLogger
from pydantic import SecretStr

from app.config import get_settings


OPENROUTER_APP_URL = "https://votrixai.com"
OPENROUTER_APP_TITLE = "Votrix Managed Agents"
OPENROUTER_MANAGEMENT_TIMEOUT_MS = 30_000


class MissingOpenRouterManagementKeyError(RuntimeError):
    """The control plane cannot provision Organization inference keys."""


class OpenRouterKeyProvisionError(RuntimeError):
    """OpenRouter did not safely provision an Organization inference key."""

    def __init__(self, message: str, *, ambiguous: bool) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class CreatedOpenRouterKey:
    key_hash: str
    key_name: str
    secret: SecretStr
    # None means the provider applies no spending cap to this key.
    limit_usd: Decimal | None
    limit_reset: str | None


@dataclass(frozen=True, slots=True)
class OpenRouterKeyUsage:
    """What one key has spent, as the provider counts it.

    These are the provider's own figures — the same ones it bills against —
    rather than a total we accumulated from what we saw. Anything spent on this
    key is in here, including calls we never observed.

    Every figure is cumulative and monotonic within its window. `usage` covers
    the key's whole life, which is the Account's whole life while a key is
    never rotated.
    """

    usage_usd: Decimal
    usage_daily_usd: Decimal
    usage_weekly_usd: Decimal
    usage_monthly_usd: Decimal
    limit_usd: Decimal | None
    limit_remaining_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class OpenRouterKeyMetadata:
    key_hash: str
    key_name: str
    disabled: bool


class OpenRouterKeyAdmin(Protocol):
    async def create_key(
        self,
        *,
        name: str,
        limit_usd: Decimal | None = None,
        limit_reset: str = "monthly",
    ) -> CreatedOpenRouterKey: ...

    async def list_keys(self, *, include_disabled: bool = True) -> list[OpenRouterKeyMetadata]: ...

    async def get_key_usage(self, key_hash: str) -> OpenRouterKeyUsage: ...

    async def disable_key(self, key_hash: str) -> None: ...

    async def update_key(
        self,
        key_hash: str,
        *,
        disabled: bool | None = None,
        limit_usd: Decimal | None = None,
        limit_reset: str | None = None,
        name: str | None = None,
    ) -> None: ...

    async def delete_key(self, key_hash: str) -> None: ...


class OpenRouterManagementClient:
    """Small typed facade over the generated OpenRouter Management SDK.

    Create is explicitly non-retrying.  OpenRouter has no documented
    idempotency key for key creation; retrying an ambiguous response can create
    a second remote key whose plaintext can never be recovered.
    """

    def __init__(self, management_key: str) -> None:
        key = management_key.strip()
        if not key:
            raise MissingOpenRouterManagementKeyError(
                "OPENROUTER_MANAGEMENT_KEY is required to provision Organization keys"
            )
        self._sdk = OpenRouter(
            api_key=key,
            http_referer=OPENROUTER_APP_URL,
            x_open_router_title=OPENROUTER_APP_TITLE,
            timeout_ms=OPENROUTER_MANAGEMENT_TIMEOUT_MS,
            # The generated SDK honors OPENROUTER_DEBUG by logging full request
            # headers and bodies. Management Authorization and one-time key
            # material must remain secret even if an operator flips that flag.
            debug_logger=NoOpLogger(),
        )

    async def create_key(
        self,
        *,
        limit_usd: Decimal | None = None,
        limit_reset: str = "monthly",
        name: str,
    ) -> CreatedOpenRouterKey:
        """Create one key, capped only if a limit is given.

        Omitting the limit is what makes a key uncapped; sending a null one is
        not the same request. ``limit_reset`` says when a cap refills, so it is
        meaningless without one and is omitted alongside it.
        """
        kwargs: dict[str, object] = {
            "name": name,
            # See the class docstring: retrying create is not safe.
            "retries": None,
        }
        if limit_usd is not None:
            kwargs["limit"] = float(limit_usd)
            kwargs["limit_reset"] = limit_reset
        provision_error: OpenRouterKeyProvisionError | None = None
        try:
            response = await self._sdk.api_keys.create_async(**kwargs)
        except Exception as exc:
            # Do not include the SDK exception text.  Some generated-client
            # errors include response bodies, and create's body contains the
            # only copy of the new plaintext secret on success.
            provision_error = OpenRouterKeyProvisionError(
                f"OpenRouter key creation failed ({type(exc).__name__})",
                # A transport failure or server-side error may happen after
                # the provider committed the key.  A normal 4xx is a definite
                # rejection and can release the local provisioning claim.
                ambiguous=_is_ambiguous_create_failure(exc),
            )
        if provision_error is not None:
            # Raise outside the provider exception handler. This removes not
            # only __cause__, but also __context__; tracing systems which walk
            # suppressed contexts cannot recover an SDK response body.
            raise provision_error

        response_error: OpenRouterKeyProvisionError | None = None
        try:
            # Redact the only plaintext copy on the SDK response immediately;
            # every value retained beyond this line is either non-secret or a
            # SecretStr whose repr is safe in traces and local-variable capture.
            secret = _take_response_secret(response)
            created = CreatedOpenRouterKey(
                key_hash=_required_string(response.data.hash, field="hash"),
                key_name=_required_string(response.data.name, field="name"),
                secret=secret,
                limit_usd=(
                    None
                    if response.data.limit is None
                    else Decimal(str(response.data.limit))
                ),
                limit_reset=_optional_string(response.data.limit_reset),
            )
            # An uncapped key must come back uncapped, and a capped one must
            # come back with the cap that was asked for. Either surprise means
            # this key does not spend the way the Account says it does.
            if created.limit_usd != limit_usd:
                raise ValueError("OpenRouter returned an unexpected key limit")
            if created.limit_usd is not None and not created.limit_usd.is_finite():
                raise ValueError("OpenRouter returned an unexpected key limit")
            if limit_usd is not None and created.limit_reset != limit_reset:
                raise ValueError("OpenRouter returned an unexpected limit reset")
        except Exception as exc:
            # A 2xx means the provider may have committed a credential even if
            # its response is malformed. Treat this as ambiguous so the
            # service reconciles by deterministic key name instead of blindly
            # creating another key.
            response_error = OpenRouterKeyProvisionError(
                f"OpenRouter key response was invalid ({type(exc).__name__})",
                ambiguous=True,
            )
        if response_error is not None:
            raise response_error
        return created

    async def list_keys(self, *, include_disabled: bool = True) -> list[OpenRouterKeyMetadata]:
        result: list[OpenRouterKeyMetadata] = []
        offset = 0
        while True:
            kwargs = {
                "include_disabled": include_disabled,
                "offset": offset,
            }
            response = await self._sdk.api_keys.list_async(**kwargs)
            page = list(response.data)
            result.extend(
                OpenRouterKeyMetadata(
                    key_hash=item.hash,
                    key_name=item.name,
                    disabled=item.disabled,
                )
                for item in page
            )
            # The Management API currently returns at most 100 keys and uses
            # offset pagination.  A short page is the terminal page.
            if len(page) < 100:
                return result
            offset += len(page)

    async def get_key_usage(self, key_hash: str) -> OpenRouterKeyUsage:
        """Read one key's counters, without listing every key to find it."""
        response = await self._sdk.api_keys.get_async(hash=key_hash)
        data = response.data
        return OpenRouterKeyUsage(
            usage_usd=_decimal(getattr(data, "usage", None)),
            usage_daily_usd=_decimal(getattr(data, "usage_daily", None)),
            usage_weekly_usd=_decimal(getattr(data, "usage_weekly", None)),
            usage_monthly_usd=_decimal(getattr(data, "usage_monthly", None)),
            limit_usd=_optional_decimal(getattr(data, "limit", None)),
            limit_remaining_usd=_optional_decimal(getattr(data, "limit_remaining", None)),
        )

    async def disable_key(self, key_hash: str) -> None:
        await self.update_key(key_hash, disabled=True)

    async def update_key(
        self,
        key_hash: str,
        *,
        disabled: bool | None = None,
        limit_usd: Decimal | None = None,
        limit_reset: str | None = None,
        name: str | None = None,
    ) -> None:
        """Update one key in place.

        Renaming keeps the key's hash, which is what usage is attributed to, so
        a name migration never splits an Account's recorded usage.
        """
        kwargs: dict[str, object] = {"hash": key_hash}
        if disabled is not None:
            kwargs["disabled"] = disabled
        if limit_usd is not None:
            kwargs["limit"] = float(limit_usd)
        if limit_reset is not None:
            kwargs["limit_reset"] = limit_reset
        if name is not None:
            kwargs["name"] = name
        await self._sdk.api_keys.update_async(**kwargs)

    async def delete_key(self, key_hash: str) -> None:
        await self._sdk.api_keys.delete_async(hash=key_hash)

    async def close(self) -> None:
        self._sdk.__exit__(None, None, None)
        await self._sdk.__aexit__(None, None, None)


_client: OpenRouterManagementClient | None = None
_client_lock = Lock()


def _decimal(value: object) -> Decimal:
    """A missing counter is zero spend, not an error.

    The provider omits a field it has nothing to report for, and a key that has
    never been used has nothing to report.
    """
    return Decimal("0") if value is None else Decimal(str(value))


def _optional_decimal(value: object) -> Decimal | None:
    """None means uncapped, which is not the same as a cap of zero."""
    return None if value is None else Decimal(str(value))


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OpenRouter key response has no {field}")
    return value.strip()


def _optional_string(value: object) -> str | None:
    """A field the provider omits when it does not apply."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _take_response_secret(response: object) -> SecretStr:
    secret = SecretStr(_required_string(getattr(response, "key", None), field="key"))
    # The generated response is a mutable Pydantic model; tests use an equally
    # mutable namespace. Keep this explicit so a future frozen SDK model fails
    # closed instead of leaving plaintext attached to an object used below.
    try:
        setattr(response, "key", "[redacted]")
    except Exception as exc:
        raise ValueError("OpenRouter key response could not be redacted") from None
    return secret


def _is_ambiguous_create_failure(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        return True
    try:
        code = int(status)
    except (TypeError, ValueError):
        return True
    return code == 408 or code >= 500


def get_openrouter_management_client() -> OpenRouterManagementClient:
    global _client
    with _client_lock:
        if _client is None:
            settings = get_settings()
            _client = OpenRouterManagementClient(settings.openrouter_management_key)
        return _client


async def close_openrouter_management_client() -> None:
    global _client
    with _client_lock:
        client = _client
        _client = None
    if client is not None:
        await client.close()


__all__ = [
    "CreatedOpenRouterKey",
    "MissingOpenRouterManagementKeyError",
    "OpenRouterKeyAdmin",
    "OpenRouterKeyMetadata",
    "OpenRouterKeyProvisionError",
    "OpenRouterManagementClient",
    "close_openrouter_management_client",
    "get_openrouter_management_client",
]

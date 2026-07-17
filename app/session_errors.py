from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


SessionErrorRetryStatus = Literal["retrying", "exhausted", "terminal"]

_RETRY_STATUSES = frozenset({"retrying", "exhausted", "terminal"})
_PUBLIC_ERROR_TYPES = frozenset(
    {
        "unknown_error",
        "model_overloaded_error",
        "model_rate_limited_error",
        "model_request_failed_error",
        "mcp_connection_failed_error",
        "mcp_authentication_failed_error",
        "billing_error",
        "credential_host_unreachable_error",
    }
)
_ERROR_TYPE_ALIASES = {
    "mcp_auth_missing": "mcp_authentication_failed_error",
    "mcp_authentication_error": "mcp_authentication_failed_error",
    "mcp_connection_error": "mcp_connection_failed_error",
    "mcp_connection_blocked": "mcp_connection_failed_error",
}


def session_error_payload(
    message: str,
    *,
    error_type: str = "unknown_error",
    retry_status: SessionErrorRetryStatus = "terminal",
    **details: Any,
) -> dict[str, Any]:
    """Build a CMA-compatible ``session.error`` while retaining VMA metadata.

    ``error_type`` remains at the top level for backwards compatibility with
    existing VMA consumers. The nested ``error`` object is the public Anthropic
    Managed Agents contract consumed by the official SDK.
    """
    return normalize_session_error_payload(
        {
            **details,
            "type": "session.error",
            "message": message,
            "error_type": error_type,
            "retry_status": retry_status,
        }
    )


def normalize_session_error_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize both current and legacy stored ``session.error`` payloads.

    This is intentionally idempotent. Applying it during persistence protects
    every producer, while applying it during serialization upgrades events that
    were stored before the nested SDK contract was implemented.
    """
    normalized = dict(payload or {})
    nested = normalized.get("error")
    nested_error = dict(nested) if isinstance(nested, Mapping) else {}

    message = str(
        nested_error.get("message")
        or normalized.get("message")
        or "Session execution failed"
    )
    legacy_error_type = str(
        normalized.get("error_type")
        or nested_error.get("type")
        or "unknown_error"
    )
    nested_public_error_type = str(nested_error.get("type") or "")
    public_error_type = (
        nested_public_error_type
        if nested_public_error_type in _PUBLIC_ERROR_TYPES
        else _public_error_type(legacy_error_type)
    )

    retry_status = _retry_status(nested_error, normalized)
    public_error: dict[str, Any] = {
        **nested_error,
        "type": public_error_type,
        "message": message,
        "retry_status": {"type": retry_status},
    }

    if public_error_type in {
        "mcp_connection_failed_error",
        "mcp_authentication_failed_error",
    }:
        server_name = nested_error.get("mcp_server_name") or normalized.get("mcp_server_name")
        if server_name:
            public_error["mcp_server_name"] = str(server_name)
        else:
            public_error["type"] = "unknown_error"
    elif public_error_type == "credential_host_unreachable_error":
        credential_id = nested_error.get("credential_id") or normalized.get("credential_id")
        vault_id = nested_error.get("vault_id") or normalized.get("vault_id")
        if credential_id and vault_id:
            public_error["credential_id"] = str(credential_id)
            public_error["vault_id"] = str(vault_id)
        else:
            public_error["type"] = "unknown_error"

    normalized.update(
        {
            "type": "session.error",
            "message": message,
            "error_type": legacy_error_type,
            "error": public_error,
        }
    )
    # ``retry_status`` is an input convenience for internal producers. The SDK
    # contract exposes it only inside ``error``.
    normalized.pop("retry_status", None)
    return normalized


def _public_error_type(error_type: str) -> str:
    candidate = _ERROR_TYPE_ALIASES.get(error_type, error_type)
    return candidate if candidate in _PUBLIC_ERROR_TYPES else "unknown_error"


def _retry_status(
    nested_error: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> SessionErrorRetryStatus:
    nested_status = nested_error.get("retry_status")
    if isinstance(nested_status, Mapping):
        candidate = nested_status.get("type")
    else:
        candidate = nested_status
    if candidate not in _RETRY_STATUSES:
        candidate = payload.get("retry_status")
    if candidate not in _RETRY_STATUSES:
        if payload.get("transient") is True:
            candidate = "retrying"
        elif str(payload.get("error_type") or "").startswith("mcp_"):
            candidate = "exhausted"
        else:
            candidate = "terminal"
    return candidate  # type: ignore[return-value]

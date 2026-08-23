"""Read provider-authoritative Session spend from OpenRouter Analytics.

The Analytics API is deliberately treated as a read model.  VMA does not copy
generation costs into its own database: callers receive the latest cumulative
snapshot OpenRouter can report for the Session at request time.

Analytics is currently beta and its dimension catalogue changes independently
of this service.  The metadata endpoint is therefore consulted before every
query instead of baking a guessed Session field name into the contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from app.utils.openrouter_management import OPENROUTER_APP_TITLE, OPENROUTER_APP_URL

OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_ANALYTICS_TIMEOUT_SECONDS = 30.0

# Request-level dimensions such as Session are documented with a 31-day query
# ceiling.  Thirty-day slices avoid that boundary while still producing one
# lifetime total for Sessions that outlive a single Analytics window.
ANALYTICS_WINDOW_DAYS = 30


class OpenRouterAnalyticsError(RuntimeError):
    """OpenRouter could not provide a complete, trustworthy usage snapshot."""


@dataclass(frozen=True, slots=True)
class OpenRouterSessionUsage:
    usage_usd: Decimal
    as_of: datetime


class OpenRouterAnalytics(Protocol):
    async def get_session_usage(
        self,
        *,
        session_id: str,
        api_key_hash: str,
        started_at: datetime,
        as_of: datetime | None = None,
    ) -> OpenRouterSessionUsage: ...


class OpenRouterAnalyticsClient:
    """Small HTTP facade around the beta Analytics endpoints.

    The generated Python SDK used for key administration does not expose the
    beta Analytics API yet, so this uses its documented HTTP surface directly.
    A management key is required; an inference key receives a provider 403.
    """

    def __init__(
        self,
        management_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = management_key.strip()
        if not key:
            raise OpenRouterAnalyticsError(
                "OPENROUTER_MANAGEMENT_KEY is required to read Session usage"
            )
        self._management_key = key
        self._transport = transport

    async def get_session_usage(
        self,
        *,
        session_id: str,
        api_key_hash: str,
        started_at: datetime,
        as_of: datetime | None = None,
    ) -> OpenRouterSessionUsage:
        observed_at = _utc(as_of or datetime.now(UTC))
        first_seen_at = min(_utc(started_at), observed_at)

        headers = {
            "Authorization": f"Bearer {self._management_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_APP_URL,
            "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
        }
        try:
            async with httpx.AsyncClient(
                base_url=OPENROUTER_API_BASE_URL,
                headers=headers,
                timeout=OPENROUTER_ANALYTICS_TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                meta = await _request_json(client, "GET", "/analytics/meta")
                schema = _object(meta.get("data"), context="Analytics metadata")
                dimensions = _catalogue(schema.get("dimensions"), context="dimensions")
                metrics = _catalogue(schema.get("metrics"), context="metrics")

                session_dimension = _catalogue_name(
                    dimensions,
                    preferred=("session_id", "session"),
                    display_label="session",
                )
                if session_dimension is None:
                    raise OpenRouterAnalyticsError(
                        "OpenRouter Analytics does not advertise a Session dimension"
                    )
                if "total_usage" not in metrics:
                    raise OpenRouterAnalyticsError(
                        "OpenRouter Analytics does not advertise total_usage"
                    )

                filters: list[dict[str, object]] = [
                    {
                        "field": session_dimension,
                        "operator": "eq",
                        "value": session_id,
                    }
                ]
                # The Session id is already globally unique, but pinning the
                # Account key as well gives the provider query the same billing
                # boundary VMA stored on the Session.
                # Only this exact field has documented hash semantics. A
                # similarly-labelled dimension may group by a display name;
                # feeding it a hash could return a trustworthy-looking zero.
                api_key_dimension = (
                    "api_key_id" if "api_key_id" in dimensions else None
                )
                if api_key_dimension is not None:
                    filters.append(
                        {
                            "field": api_key_dimension,
                            "operator": "eq",
                            # Analytics accepts the SHA-256 hash returned by the
                            # key-management API and resolves it provider-side.
                            "value": api_key_hash,
                        }
                    )

                total = Decimal("0")
                for window_start, window_end in _windows(first_seen_at, observed_at):
                    result = await _request_json(
                        client,
                        "POST",
                        "/analytics/query",
                        json={
                            "metrics": ["total_usage"],
                            "filters": filters,
                            "time_range": {
                                "start": _iso(window_start),
                                "end": _iso(window_end),
                            },
                            # With no dimension or granularity this is one
                            # aggregate row. More than one means the response no
                            # longer matches the query contract and must not be
                            # silently summed into a bill.
                            "limit": 1,
                        },
                    )
                    total += _usage_total(result)
        except OpenRouterAnalyticsError:
            raise
        except httpx.HTTPError as exc:
            raise OpenRouterAnalyticsError(
                f"OpenRouter Analytics request failed ({type(exc).__name__})"
            ) from None

        return OpenRouterSessionUsage(usage_usd=total, as_of=observed_at)


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    try:
        response = await client.request(method, path, json=json)
    except httpx.HTTPError:
        raise
    if not response.is_success:
        # Do not include the response body. Provider errors can echo request
        # details, while callers only need to know this snapshot is unavailable.
        raise OpenRouterAnalyticsError(
            f"OpenRouter Analytics returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError:
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics returned invalid JSON"
        ) from None
    return _object(payload, context="Analytics response")


def _catalogue(value: object, *, context: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise OpenRouterAnalyticsError(f"OpenRouter Analytics omitted {context}")
    result: dict[str, str] = {}
    for item in value:
        row = _object(item, context=f"Analytics {context} entry")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise OpenRouterAnalyticsError(
                f"OpenRouter Analytics returned an invalid {context} entry"
            )
        label = row.get("display_label")
        result[name.strip()] = label.strip() if isinstance(label, str) else ""
    return result


def _catalogue_name(
    catalogue: Mapping[str, str],
    *,
    preferred: tuple[str, ...],
    display_label: str,
) -> str | None:
    for candidate in preferred:
        if candidate in catalogue:
            return candidate
    wanted = display_label.casefold()
    return next(
        (name for name, label in catalogue.items() if label.casefold() == wanted),
        None,
    )


def _usage_total(payload: Mapping[str, Any]) -> Decimal:
    envelope = _object(payload.get("data"), context="Analytics query data")
    metadata = _object(envelope.get("metadata"), context="Analytics query metadata")
    truncated = metadata.get("truncated")
    if truncated is True:
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics truncated the Session usage query"
        )
    if truncated is not False:
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics did not confirm the Session usage query was complete"
        )

    rows = envelope.get("data")
    if not isinstance(rows, list):
        raise OpenRouterAnalyticsError("OpenRouter Analytics omitted query rows")
    if len(rows) > 1:
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics returned multiple aggregate rows"
        )
    if not rows:
        return Decimal("0")

    row = _object(rows[0], context="Analytics query row")
    value = row.get("total_usage")
    if value is None:
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics omitted total_usage from its aggregate row"
        )
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics returned an invalid total_usage"
        ) from None
    if not amount.is_finite() or amount < 0:
        raise OpenRouterAnalyticsError(
            "OpenRouter Analytics returned an invalid total_usage"
        )
    return amount


def _object(value: object, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenRouterAnalyticsError(f"{context} is not an object")
    return value


def _windows(start: datetime, end: datetime):
    if start == end:
        # A just-created Session still needs a valid provider range. There can
        # be no completed generation in the microsecond before its creation.
        start = end - timedelta(microseconds=1)
    cursor = start
    width = timedelta(days=ANALYTICS_WINDOW_DAYS)
    while cursor < end:
        boundary = min(cursor + width, end)
        yield cursor, boundary
        cursor = boundary


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "OpenRouterAnalytics",
    "OpenRouterAnalyticsClient",
    "OpenRouterAnalyticsError",
    "OpenRouterSessionUsage",
]

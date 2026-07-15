"""Local, best-effort E2B runtime cost estimates.

This module deliberately does not call E2B, create billable usage records, or
promise invoice-grade accuracy.  It only accumulates locally observed running
intervals in the private ``SessionSandbox.config`` JSON document.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.config import get_settings

DEFAULT_E2B_VCPU_SECOND_USD = Decimal("0.000014")
DEFAULT_E2B_GIB_SECOND_USD = Decimal("0.0000045")
E2B_COST_ESTIMATE_CONFIG_KEY = "_vma_e2b_cost_estimate"
E2B_COST_ESTIMATE_SCHEMA = "vma-e2b-local-cost-v1"


@dataclass(frozen=True, slots=True)
class E2BCostProfile:
    """Resources and rates frozen at the start of one local interval."""

    vcpu_count: Decimal
    memory_mb: int
    vcpu_second_usd: Decimal
    gib_second_usd: Decimal


@dataclass(frozen=True, slots=True)
class E2BCostSummary:
    """Private operational estimate, including a currently open interval."""

    runtime_ms: int
    estimated_usd: Decimal
    running: bool
    running_started_at: datetime | None


def estimate_e2b_cost_usd(
    *,
    runtime_ms: int,
    vcpu_count: int | Decimal,
    memory_mb: int,
    vcpu_second_usd: Decimal = DEFAULT_E2B_VCPU_SECOND_USD,
    gib_second_usd: Decimal = DEFAULT_E2B_GIB_SECOND_USD,
) -> Decimal:
    """Estimate compute cost from allocated resources and elapsed time."""
    if runtime_ms < 0:
        raise ValueError("runtime_ms must be non-negative")
    cpu = _decimal(vcpu_count, field="vcpu_count")
    cpu_rate = _decimal(vcpu_second_usd, field="vcpu_second_usd")
    memory_rate = _decimal(gib_second_usd, field="gib_second_usd")
    if cpu < 0 or memory_mb < 0 or cpu_rate < 0 or memory_rate < 0:
        raise ValueError("E2B resources and rates must be non-negative")

    seconds = Decimal(runtime_ms) / Decimal(1000)
    memory_gib = Decimal(memory_mb) / Decimal(1024)
    return seconds * ((cpu * cpu_rate) + (memory_gib * memory_rate))


def configured_e2b_cost_profile() -> E2BCostProfile | None:
    """Return the configured estimate profile, or ``None`` when unavailable."""
    settings = get_settings()
    if not settings.vma_e2b_cost_estimation_enabled:
        return None
    resources = dict(settings.vma_e2b_template_resources or {})
    try:
        cpu = _decimal(resources.get("cpu"), field="VMA_E2B_TEMPLATE_RESOURCES.cpu")
        memory_mb = _integer(
            resources.get("memory_mb"),
            field="VMA_E2B_TEMPLATE_RESOURCES.memory_mb",
        )
    except (TypeError, ValueError):
        # Running E2B must not fail merely because optional cost metadata is
        # unavailable. Operators can add the resource profile and estimate new
        # intervals without a schema migration.
        return None
    if cpu <= 0 or memory_mb <= 0:
        return None
    return E2BCostProfile(
        vcpu_count=cpu,
        memory_mb=memory_mb,
        vcpu_second_usd=settings.vma_e2b_vcpu_second_usd,
        gib_second_usd=settings.vma_e2b_gib_second_usd,
    )


def begin_e2b_cost_interval(
    config: Mapping[str, Any] | None,
    *,
    profile: E2BCostProfile | None,
    at: datetime,
) -> dict[str, Any]:
    """Open one interval unless an earlier idempotent call already did so."""
    updated = dict(config or {})
    if profile is None:
        return updated
    metadata = _metadata(updated)
    if _parse_datetime(metadata.get("running_started_at")) is not None:
        return updated

    metadata.update(
        {
            "schema": E2B_COST_ESTIMATE_SCHEMA,
            "runtime_ms": _non_negative_int(metadata.get("runtime_ms")),
            "estimated_usd": _decimal_string(
                _non_negative_decimal(metadata.get("estimated_usd"))
            ),
            "running_started_at": _aware_utc(at).isoformat(),
            "active_profile": _profile_to_json(profile),
        }
    )
    updated[E2B_COST_ESTIMATE_CONFIG_KEY] = metadata
    return updated


def end_e2b_cost_interval(
    config: Mapping[str, Any] | None,
    *,
    at: datetime,
) -> dict[str, Any]:
    """Close one interval exactly once and accumulate its local estimate."""
    updated = dict(config or {})
    metadata = _metadata(updated)
    started_at = _parse_datetime(metadata.get("running_started_at"))
    profile = _profile_from_json(metadata.get("active_profile"))
    if started_at is None or profile is None:
        return updated

    elapsed_ms = _elapsed_ms(started_at, at)
    runtime_ms = _non_negative_int(metadata.get("runtime_ms")) + elapsed_ms
    estimated_usd = _non_negative_decimal(metadata.get("estimated_usd"))
    estimated_usd += estimate_e2b_cost_usd(
        runtime_ms=elapsed_ms,
        vcpu_count=profile.vcpu_count,
        memory_mb=profile.memory_mb,
        vcpu_second_usd=profile.vcpu_second_usd,
        gib_second_usd=profile.gib_second_usd,
    )
    metadata.update(
        {
            "schema": E2B_COST_ESTIMATE_SCHEMA,
            "runtime_ms": runtime_ms,
            "estimated_usd": _decimal_string(estimated_usd),
            "running_started_at": None,
            "active_profile": None,
            "last_profile": _profile_to_json(profile),
        }
    )
    updated[E2B_COST_ESTIMATE_CONFIG_KEY] = metadata
    return updated


def e2b_cost_summary(
    config: Mapping[str, Any] | None,
    *,
    at: datetime | None = None,
) -> E2BCostSummary | None:
    """Read the private estimate and project an open interval through ``at``."""
    metadata = _metadata(dict(config or {}))
    if metadata.get("schema") != E2B_COST_ESTIMATE_SCHEMA:
        return None

    runtime_ms = _non_negative_int(metadata.get("runtime_ms"))
    estimated_usd = _non_negative_decimal(metadata.get("estimated_usd"))
    started_at = _parse_datetime(metadata.get("running_started_at"))
    profile = _profile_from_json(metadata.get("active_profile"))
    if started_at is not None and profile is not None:
        elapsed_ms = _elapsed_ms(started_at, at or datetime.now(timezone.utc))
        runtime_ms += elapsed_ms
        estimated_usd += estimate_e2b_cost_usd(
            runtime_ms=elapsed_ms,
            vcpu_count=profile.vcpu_count,
            memory_mb=profile.memory_mb,
            vcpu_second_usd=profile.vcpu_second_usd,
            gib_second_usd=profile.gib_second_usd,
        )
    return E2BCostSummary(
        runtime_ms=runtime_ms,
        estimated_usd=estimated_usd,
        running=started_at is not None and profile is not None,
        running_started_at=started_at,
    )


def session_sandbox_cost_summary(
    sandbox: Any,
    *,
    at: datetime | None = None,
) -> E2BCostSummary | None:
    """Inspect a private E2B sandbox estimate without exposing a public API."""
    if str(getattr(sandbox, "provider", "")) != "e2b":
        return None
    return e2b_cost_summary(getattr(sandbox, "config", None), at=at)


def _metadata(config: Mapping[str, Any]) -> dict[str, Any]:
    value = config.get(E2B_COST_ESTIMATE_CONFIG_KEY)
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _profile_to_json(profile: E2BCostProfile) -> dict[str, Any]:
    return {
        "vcpu_count": _decimal_string(profile.vcpu_count),
        "memory_mb": profile.memory_mb,
        "vcpu_second_usd": _decimal_string(profile.vcpu_second_usd),
        "gib_second_usd": _decimal_string(profile.gib_second_usd),
    }


def _profile_from_json(value: Any) -> E2BCostProfile | None:
    if not isinstance(value, Mapping):
        return None
    try:
        profile = E2BCostProfile(
            vcpu_count=_decimal(value.get("vcpu_count"), field="vcpu_count"),
            memory_mb=_integer(value.get("memory_mb"), field="memory_mb"),
            vcpu_second_usd=_decimal(
                value.get("vcpu_second_usd"), field="vcpu_second_usd"
            ),
            gib_second_usd=_decimal(
                value.get("gib_second_usd"), field="gib_second_usd"
            ),
        )
    except (TypeError, ValueError):
        return None
    if (
        profile.vcpu_count < 0
        or profile.memory_mb < 0
        or profile.vcpu_second_usd < 0
        or profile.gib_second_usd < 0
    ):
        return None
    return profile


def _decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _integer(value: Any, *, field: str) -> int:
    number = _decimal(value, field=field)
    if number != number.to_integral_value():
        raise ValueError(f"{field} must be an integer")
    return int(number)


def _non_negative_int(value: Any) -> int:
    try:
        result = _integer(value if value is not None else 0, field="runtime_ms")
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _non_negative_decimal(value: Any) -> Decimal:
    try:
        result = _decimal(value if value is not None else 0, field="estimated_usd")
    except (TypeError, ValueError):
        return Decimal(0)
    return max(Decimal(0), result)


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_ms(started_at: datetime, ended_at: datetime) -> int:
    elapsed = _aware_utc(ended_at) - _aware_utc(started_at)
    microseconds = (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    return max(0, microseconds // 1000)


__all__ = [
    "DEFAULT_E2B_GIB_SECOND_USD",
    "DEFAULT_E2B_VCPU_SECOND_USD",
    "E2B_COST_ESTIMATE_CONFIG_KEY",
    "E2BCostProfile",
    "E2BCostSummary",
    "begin_e2b_cost_interval",
    "configured_e2b_cost_profile",
    "e2b_cost_summary",
    "end_e2b_cost_interval",
    "estimate_e2b_cost_usd",
    "session_sandbox_cost_summary",
]

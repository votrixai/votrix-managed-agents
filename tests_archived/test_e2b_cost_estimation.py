from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.config import get_settings
from app.runtime.e2b_cost_estimation import (
    E2BCostProfile,
    E2B_COST_ESTIMATE_CONFIG_KEY,
    begin_e2b_cost_interval,
    configured_e2b_cost_profile,
    e2b_cost_summary,
    end_e2b_cost_interval,
    estimate_e2b_cost_usd,
    session_sandbox_cost_summary,
)


def _profile() -> E2BCostProfile:
    return E2BCostProfile(
        vcpu_count=Decimal(2),
        memory_mb=1024,
        vcpu_second_usd=Decimal("0.000014"),
        gib_second_usd=Decimal("0.0000045"),
    )


def test_e2b_default_formula_estimates_two_vcpu_one_gib_at_0117_per_hour():
    assert estimate_e2b_cost_usd(
        runtime_ms=3_600_000,
        vcpu_count=2,
        memory_mb=1024,
    ) == Decimal("0.117")


def test_e2b_cost_estimate_accumulates_multiple_intervals_once():
    started_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    config = begin_e2b_cost_interval({}, profile=_profile(), at=started_at)
    config = end_e2b_cost_interval(
        config,
        at=started_at + timedelta(milliseconds=1250),
    )
    config = begin_e2b_cost_interval(
        config,
        profile=_profile(),
        at=started_at + timedelta(seconds=10),
    )
    config = end_e2b_cost_interval(
        config,
        at=started_at + timedelta(seconds=12, milliseconds=750),
    )

    summary = e2b_cost_summary(config)
    assert summary is not None
    assert summary.runtime_ms == 4000
    assert summary.estimated_usd == Decimal("0.0001300")
    assert summary.running is False

    serialized = json.loads(json.dumps(config))
    metadata = serialized[E2B_COST_ESTIMATE_CONFIG_KEY]
    assert metadata["runtime_ms"] == 4000
    assert isinstance(metadata["estimated_usd"], str)
    assert Decimal(metadata["estimated_usd"]) == Decimal("0.0001300")
    assert e2b_cost_summary(serialized) == summary

    repeated = end_e2b_cost_interval(
        config,
        at=started_at + timedelta(hours=1),
    )
    assert repeated == config
    assert e2b_cost_summary(repeated) == summary


def test_e2b_open_interval_summary_projects_without_mutating_metadata():
    started_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    config = begin_e2b_cost_interval({}, profile=_profile(), at=started_at)

    summary = e2b_cost_summary(
        config,
        at=started_at + timedelta(seconds=2),
    )

    assert summary is not None
    assert summary.running is True
    assert summary.runtime_ms == 2000
    assert summary.estimated_usd == Decimal("0.0000650")
    assert e2b_cost_summary(config, at=started_at).runtime_ms == 0  # type: ignore[union-attr]


def test_e2b_cost_estimation_can_be_disabled_and_ignores_other_providers(monkeypatch):
    monkeypatch.setenv("VMA_E2B_COST_ESTIMATION_ENABLED", "false")
    monkeypatch.setenv("VMA_E2B_TEMPLATE_RESOURCES", '{"cpu":2,"memory_mb":1024}')
    get_settings.cache_clear()

    assert configured_e2b_cost_profile() is None
    untouched = begin_e2b_cost_interval(
        {"sealed": True},
        profile=configured_e2b_cost_profile(),
        at=datetime.now(timezone.utc),
    )
    assert untouched == {"sealed": True}
    assert session_sandbox_cost_summary(
        SimpleNamespace(provider="state", config=untouched)
    ) is None


def test_e2b_cost_profile_uses_configurable_rates(monkeypatch):
    monkeypatch.setenv("VMA_E2B_COST_ESTIMATION_ENABLED", "true")
    monkeypatch.setenv("VMA_E2B_TEMPLATE_RESOURCES", '{"cpu":4,"memory_mb":8192}')
    monkeypatch.setenv("VMA_E2B_VCPU_SECOND_USD", "0.1")
    monkeypatch.setenv("VMA_E2B_GIB_SECOND_USD", "0.2")
    get_settings.cache_clear()

    profile = configured_e2b_cost_profile()

    assert profile == E2BCostProfile(
        vcpu_count=Decimal(4),
        memory_mb=8192,
        vcpu_second_usd=Decimal("0.1"),
        gib_second_usd=Decimal("0.2"),
    )

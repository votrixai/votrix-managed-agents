from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import organization_funding as funding_q
from app.secret_cipher import ENCRYPTED_PREFIX
from scripts import provision_organization_funding as provisioner


TEST_ENCRYPTION_KEY = base64.b64encode(b"p" * 32).decode()


@pytest.fixture
def funding_encryption_key(monkeypatch):
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_provisioning_creates_then_rotates_exact_binding_without_secret_output(
    funding_encryption_key,
) -> None:
    trial_expiry = datetime.now(timezone.utc) + timedelta(days=14)
    key_expiry = datetime.now(timezone.utc) + timedelta(days=7)
    created = await provisioner.provision_organization_funding(
        organization_id="org_test",
        provider="OpenRouter",
        api_key="provider-secret-v1",
        policy="platform_only",
        account_status="active",
        trial_expires_at=trial_expiry,
        upstream_key_id="upstream-v1",
        spending_limit_usd_micros=5_000_000,
        provider_key_expires_at=key_expiry,
    )

    assert created.provider_key_action == "created"
    assert created.provider == "openrouter"
    safe_output = json.dumps(created.to_dict(), sort_keys=True)
    assert "provider-secret-v1" not in safe_output
    assert ENCRYPTED_PREFIX not in safe_output
    assert "encrypted_api_key" not in safe_output

    async with session_scope() as db:
        stored = await funding_q.load_organization_provider_key_binding_by_id(
            db,
            organization_id="org_test",
            provider_key_binding_id=created.provider_key_binding_id,
        )
        assert stored is not None
        assert stored.encrypted_api_key.startswith(ENCRYPTED_PREFIX)
        assert "provider-secret-v1" not in stored.encrypted_api_key

    rotated = await provisioner.provision_organization_funding(
        organization_id="org_test",
        provider="openrouter",
        api_key="provider-secret-v2",
        policy="prefer_platform",
        upstream_key_id="upstream-v2",
        spending_limit_usd_micros=8_000_000,
    )

    assert rotated.provider_key_action == "rotated"
    assert rotated.provider_key_binding_id == created.provider_key_binding_id
    assert rotated.account_id == created.account_id
    assert rotated.policy == "prefer_platform"
    assert rotated.upstream_key_id == "upstream-v2"
    assert rotated.spending_limit_usd_micros == 8_000_000
    # Omitted rotation metadata remains unchanged.
    assert provisioner._format_datetime(
        rotated.provider_key_expires_at
    ) == provisioner._format_datetime(key_expiry)

    async with session_scope() as db:
        plaintext = await funding_q._load_active_organization_provider_api_key(
            db,
            organization_id="org_test",
            provider="openrouter",
            provider_key_binding_id=rotated.provider_key_binding_id,
            organization_billing_account_id=rotated.account_id,
        )
    assert plaintext == "provider-secret-v2"


async def test_provisioning_updates_account_and_clears_optional_metadata(
    funding_encryption_key,
) -> None:
    await provisioner.provision_organization_funding(
        organization_id="org_test",
        provider="openrouter",
        api_key="provider-secret-v1",
        policy="platform_only",
        trial_expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        upstream_key_id="upstream-v1",
        spending_limit_usd_micros=1_000_000,
        provider_key_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    updated = await provisioner.provision_organization_funding(
        organization_id="org_test",
        provider="openrouter",
        api_key="provider-secret-v2",
        policy="byok_only",
        account_status="suspended",
        trial_expires_at=None,
        upstream_key_id=None,
        spending_limit_usd_micros=None,
        provider_key_expires_at=None,
    )

    assert updated.account_status == "suspended"
    assert updated.policy == "byok_only"
    assert updated.trial_expires_at is None
    assert updated.upstream_key_id is None
    assert updated.spending_limit_usd_micros is None
    assert updated.provider_key_expires_at is None


async def test_provisioning_requires_an_existing_active_organization(
    funding_encryption_key,
) -> None:
    with pytest.raises(
        provisioner.OrganizationFundingProvisionError,
        match="does not exist",
    ):
        await provisioner.provision_organization_funding(
            organization_id="org_missing_funding",
            provider="openrouter",
            api_key="provider-secret",
        )


def test_provider_api_key_sources_never_require_a_plaintext_cli_value() -> None:
    assert provisioner.read_provider_api_key(
        environment_variable="SAFE_PROVIDER_KEY",
        environ={"SAFE_PROVIDER_KEY": "environment-secret"},
    ) == "environment-secret"
    assert provisioner.read_provider_api_key(
        read_stdin=True,
        stdin=StringIO("stdin-secret\n"),
        environ={},
    ) == "stdin-secret"
    assert provisioner.read_provider_api_key(
        environment_variable="MISSING_PROVIDER_KEY",
        environ={},
        prompt=lambda _message: "prompt-secret",
    ) == "prompt-secret"

    option_strings = {
        option
        for action in provisioner._parser()._actions
        for option in action.option_strings
    }
    assert "--api-key" not in option_strings
    assert {"--api-key-env", "--api-key-stdin"} <= option_strings


def test_cli_prints_metadata_only(monkeypatch, capsys) -> None:
    expected = provisioner.OrganizationFundingProvisionResult(
        organization_id="org_test",
        account_id="billacct_test",
        account_status="active",
        policy="platform_only",
        currency="USD",
        trial_expires_at=None,
        provider_key_binding_id="providerkey_test",
        provider="openrouter",
        provider_key_status="active",
        upstream_key_id="upstream-test",
        spending_limit_usd_micros=5_000_000,
        provider_key_expires_at=None,
        provider_key_action="created",
    )

    async def fake_run(args, *, api_key):
        assert args.organization_id == "org_test"
        assert api_key == "operator-secret"
        return expected

    monkeypatch.setattr(provisioner, "_run", fake_run)
    monkeypatch.setenv(provisioner.DEFAULT_API_KEY_ENV, "operator-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision_organization_funding",
            "--organization-id",
            "org_test",
            "--provider",
            "openrouter",
        ],
    )

    provisioner.main()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "operator-secret" not in captured.out
    assert ENCRYPTED_PREFIX not in captured.out
    assert json.loads(captured.out) == expected.to_dict()


@pytest.mark.parametrize(
    "value",
    ["2026-07-16T12:00:00", "not-a-timestamp"],
)
def test_cli_rejects_invalid_or_naive_timestamps(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        provisioner._parse_datetime(value)

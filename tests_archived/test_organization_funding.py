from __future__ import annotations

import base64
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Organization
from app.db.queries import organization_funding as funding_q
from app.secret_cipher import ENCRYPTED_PREFIX


TEST_ENCRYPTION_KEY = base64.b64encode(b"f" * 32).decode()


@pytest.fixture
def funding_encryption_key(monkeypatch):
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_organization_provider_key_is_encrypted_scoped_and_rotated_in_place(
    funding_encryption_key,
) -> None:
    async with session_scope() as db:
        account = await funding_q.create_organization_billing_account(
            db,
            policy="prefer_byok",
            trial_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        binding = await funding_q.create_organization_provider_key_binding(
            db,
            organization_billing_account_id=account.id,
            provider="OpenRouter",
            api_key="platform-key-v1",
            upstream_key_id="upstream-1",
            spending_limit_usd_micros=5_000_000,
        )
        binding_id = binding.id
        await db.commit()

    assert account.currency == "USD"
    assert binding.provider == "openrouter"
    assert binding.encrypted_api_key.startswith(ENCRYPTED_PREFIX)
    assert "platform-key-v1" not in binding.encrypted_api_key

    async with session_scope() as db:
        loaded = await funding_q.load_organization_provider_key_binding(
            db,
            provider="openrouter",
        )
        assert loaded is not None
        assert loaded.id == binding_id
        assert loaded.encrypted_api_key.startswith(ENCRYPTED_PREFIX)
        assert (
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
                provider_key_binding_id=binding_id,
                organization_billing_account_id=account.id,
            )
            == "platform-key-v1"
        )

        rotated = await funding_q.rotate_organization_provider_key_binding(
            db,
            provider="openrouter",
            api_key="platform-key-v2",
            upstream_key_id="upstream-2",
            spending_limit_usd_micros=8_000_000,
        )
        await db.commit()

    assert rotated.id == binding_id
    assert rotated.upstream_key_id == "upstream-2"
    assert rotated.spending_limit_usd_micros == 8_000_000
    assert rotated.encrypted_api_key.startswith(ENCRYPTED_PREFIX)
    assert "platform-key-v2" not in rotated.encrypted_api_key

    async with session_scope() as db:
        assert (
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
                provider_key_binding_id=binding_id,
            )
            == "platform-key-v2"
        )
        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="does not match the Session provider",
        ):
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="anthropic",
                provider_key_binding_id=binding_id,
            )
        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="does not match the Session billing account",
        ):
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
                provider_key_binding_id=binding_id,
                organization_billing_account_id="billacct_wrong",
            )


async def test_organization_funding_records_are_unique_and_organization_scoped(
    funding_encryption_key,
) -> None:
    async with session_scope() as db:
        account = await funding_q.create_organization_billing_account(db)
        await funding_q.create_organization_provider_key_binding(
            db,
            organization_billing_account_id=account.id,
            provider="openrouter",
            api_key="organization-a-key",
        )
        with pytest.raises(
            funding_q.OrganizationFundingConflictError,
            match="already has a billing account",
        ):
            await funding_q.create_organization_billing_account(db)
        with pytest.raises(
            funding_q.OrganizationFundingConflictError,
            match="already has a openrouter provider key binding",
        ):
            await funding_q.create_organization_provider_key_binding(
                db,
                organization_billing_account_id=account.id,
                provider="openrouter",
                api_key="duplicate-key",
            )

        db.add(
            Organization(
                id="org_funding_b",
                slug="funding-b",
                name="Funding B",
                metadata_={},
            )
        )
        await db.flush()
        other_account = await funding_q.create_organization_billing_account(
            db,
            organization_id="org_funding_b",
        )
        await funding_q.create_organization_provider_key_binding(
            db,
            organization_id="org_funding_b",
            organization_billing_account_id=other_account.id,
            provider="openrouter",
            api_key="organization-b-key",
        )
        await db.commit()

    async with session_scope() as db:
        own_binding = await funding_q.load_organization_provider_key_binding(
            db,
            provider="openrouter",
        )
        other_binding = await funding_q.load_organization_provider_key_binding(
            db,
            organization_id="org_funding_b",
            provider="openrouter",
        )
        assert own_binding is not None
        assert other_binding is not None
        assert own_binding.id != other_binding.id
        assert (
            await funding_q._load_active_organization_provider_api_key(
                db,
                organization_id="org_funding_b",
                provider="openrouter",
            )
            == "organization-b-key"
        )

        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="billing account was not found",
        ):
            await funding_q.create_organization_provider_key_binding(
                db,
                organization_id="org_funding_b",
                organization_billing_account_id=account.id,
                provider="anthropic",
                api_key="cross-organization-key",
            )


async def test_active_provider_key_load_fails_closed_for_lifecycle_and_expiry(
    funding_encryption_key,
) -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with session_scope() as db:
        account = await funding_q.create_organization_billing_account(
            db,
            trial_expires_at=future,
        )
        await funding_q.create_organization_provider_key_binding(
            db,
            organization_billing_account_id=account.id,
            provider="openrouter",
            api_key="active-key",
            expires_at=future,
        )
        await db.commit()

    async with session_scope() as db:
        await funding_q.update_organization_billing_account(
            db,
            status="suspended",
        )
        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="billing account is not active",
        ):
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
            )

        await funding_q.update_organization_billing_account(
            db,
            status="active",
            trial_expires_at=past,
        )
        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="trial funding has expired",
        ):
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
            )

        await funding_q.update_organization_billing_account(
            db,
            trial_expires_at=future,
        )
        await funding_q.update_organization_provider_key_binding(
            db,
            provider="openrouter",
            status="revoked",
        )
        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="provider key binding is not active",
        ):
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
            )

        await funding_q.update_organization_provider_key_binding(
            db,
            provider="openrouter",
            status="active",
            expires_at=past,
        )
        with pytest.raises(
            funding_q.OrganizationFundingUnavailableError,
            match="provider key binding has expired",
        ):
            await funding_q._load_active_organization_provider_api_key(
                db,
                provider="openrouter",
            )


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda: {"policy": "invalid"}, "funding policy"),
        (lambda: {"status": "invalid"}, "billing account status"),
    ],
)
async def test_billing_account_values_are_validated(operation, message) -> None:
    async with session_scope() as db:
        with pytest.raises(ValueError, match=message):
            await funding_q.create_organization_billing_account(db, **operation())


async def test_provider_key_values_are_validated(funding_encryption_key) -> None:
    async with session_scope() as db:
        account = await funding_q.create_organization_billing_account(db)
        with pytest.raises(ValueError, match="non-negative integer"):
            await funding_q.create_organization_provider_key_binding(
                db,
                organization_billing_account_id=account.id,
                provider="openrouter",
                api_key="key",
                spending_limit_usd_micros=-1,
            )
        with pytest.raises(ValueError, match="API key is required"):
            await funding_q.create_organization_provider_key_binding(
                db,
                organization_billing_account_id=account.id,
                provider="openrouter",
                api_key=" ",
            )


def test_organization_funding_migration_upgrade_check_and_downgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "organization-funding-migration.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "organization_billing_accounts" in tables
        assert "organization_provider_key_bindings" in tables

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20260716_0016"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "organization_billing_accounts" not in tables
    assert "organization_provider_key_bindings" not in tables
    assert "session_funding_bindings" in tables

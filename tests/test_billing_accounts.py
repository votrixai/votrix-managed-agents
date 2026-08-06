"""Billing Accounts: what one is, and what it refuses to become."""

from __future__ import annotations

import base64
import os
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.config import get_settings
from app.db.models.organization_accounts import (
    ACCOUNT_ACTIVE,
    ACCOUNT_SUSPENDED,
    CREDENTIAL_ACTIVE,
    CREDENTIAL_SUSPENDED,
)
from app.db.queries import organization_accounts as accounts_q
from app.integrations.openrouter_management import (
    CreatedOpenRouterKey,
    OpenRouterKeyMetadata,
)
from app.models.errors import Conflict, InvalidRequest, NotFound
from app.services import accounts as org_service
from app.services import organization_accounts as service

TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode().rstrip("=")


class FakeKeys:
    """Stands in for the provider's key management API.

    Records what was asked for, because the name and the limit are the two
    things this service is responsible for getting right at the provider.
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, Decimal | None]] = []
        self.updates: list[tuple[str, bool | None]] = []
        self.deleted: list[str] = []

    async def create_key(self, *, name, limit_usd=None, limit_reset="monthly"):
        self.created.append((name, limit_usd))
        return CreatedOpenRouterKey(
            key_hash=f"hash-{len(self.created)}",
            key_name=name,
            secret=SecretStr(f"sk-or-v1-{len(self.created)}"),
            limit_usd=limit_usd,
            limit_reset=limit_reset if limit_usd is not None else None,
        )

    async def list_keys(self, *, include_disabled: bool = True):
        return [
            OpenRouterKeyMetadata(
                key_hash=f"hash-{index}", key_name=name, disabled=False
            )
            for index, (name, _) in enumerate(self.created, start=1)
        ]

    async def disable_key(self, key_hash: str) -> None:
        self.updates.append((key_hash, True))

    async def update_key(
        self, key_hash, *, disabled=None, limit_usd=None, limit_reset=None, name=None
    ) -> None:
        self.updates.append((key_hash, disabled))

    async def delete_key(self, key_hash: str) -> None:  # pragma: no cover
        self.deleted.append(key_hash)


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """A cipher key, because a credential is never stored in the clear."""
    monkeypatch.setenv("VMA_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    service._cipher.cache_clear()
    yield
    get_settings.cache_clear()
    service._cipher.cache_clear()


async def test_creating_an_account_mints_a_key_named_after_it(db, org):
    keys = FakeKeys()

    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )

    assert account.status == ACCOUNT_ACTIVE
    # The provider can only attribute what its own console shows, and the
    # Account id is the whole of that attribution.
    name, limit = keys.created[0]
    assert name == f"vma:test:org:{org}:acct:{account.id}:key:1"
    assert limit is None
    assert account.credential is not None
    assert account.credential.status == CREDENTIAL_ACTIVE


async def test_the_stored_credential_is_never_the_plaintext_key(db, org):
    keys = FakeKeys()

    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )

    stored = account.credential.encrypted_key
    assert stored != "sk-or-v1-1"
    assert "sk-or-v1" not in stored
    # Round-trips under the same Organization and key, and not under another:
    # the pair is bound into the ciphertext rather than trusted alongside it.
    assert (
        service._cipher().decrypt(
            stored, organization_id=org, key_hash=account.credential.key_hash
        )
        == "sk-or-v1-1"
    )


async def test_a_limit_is_carried_to_the_provider(db, org):
    """The cap is only real where the provider enforces it."""
    keys = FakeKeys()

    account = await service.create_account(
        db,
        organization_id=org,
        name="Capped",
        limit_usd=Decimal("25"),
        keys=keys,
    )

    assert keys.created[0][1] == Decimal("25")
    assert account.limit_usd == Decimal("25")


async def test_an_account_is_uncapped_unless_a_limit_is_asked_for(db, org):
    keys = FakeKeys()

    account = await service.create_account(
        db, organization_id=org, name="Open", keys=keys
    )

    assert account.limit_usd is None
    assert keys.created[0][1] is None


async def test_a_repeated_idempotency_key_returns_the_first_account(db, org):
    """Otherwise a retried create bills the same thing twice, on two keys."""
    keys = FakeKeys()

    first = await service.create_account(
        db, organization_id=org, name="Research", idempotency_key="abc", keys=keys
    )
    second = await service.create_account(
        db, organization_id=org, name="Research", idempotency_key="abc", keys=keys
    )

    assert first.id == second.id
    assert len(keys.created) == 1


async def test_suspending_disables_the_key_and_keeps_the_account(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    key_hash = account.credential.key_hash

    suspended = await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert suspended.status == ACCOUNT_SUSPENDED
    assert suspended.credential.status == CREDENTIAL_SUSPENDED
    # Disabled, not deleted — what it spent stays attributable to this key.
    assert keys.updates == [(key_hash, True)]
    assert keys.deleted == []


async def test_resuming_puts_the_same_credential_back_to_work(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    key_hash = account.credential.key_hash
    await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    resumed = await service.resume_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert resumed.status == ACCOUNT_ACTIVE
    assert resumed.credential.key_hash == key_hash
    assert keys.updates[-1] == (key_hash, False)
    assert len(keys.created) == 1


async def test_suspending_twice_is_not_an_error(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    again = await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert again.status == ACCOUNT_SUSPENDED
    assert len(keys.updates) == 1


async def test_resuming_an_account_that_was_never_suspended_is_refused(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )

    resumed = await service.resume_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert resumed.status == ACCOUNT_ACTIVE
    assert keys.updates == []


async def test_an_account_belonging_to_another_organization_is_not_found(db, org):
    keys = FakeKeys()
    from app.db.queries import accounts as accounts_query

    other = await accounts_query.create_organization(db, slug="other", name="Other")
    await db.commit()
    account = await service.create_account(
        db, organization_id=other.id, name="Theirs", keys=keys
    )

    with pytest.raises(NotFound):
        await service.get_account(db, organization_id=org, account_id=account.id)


async def test_a_nameless_account_is_refused_before_any_key_is_minted(db, org):
    keys = FakeKeys()

    with pytest.raises(InvalidRequest):
        await service.create_account(db, organization_id=org, name="   ", keys=keys)

    assert keys.created == []


async def test_a_zero_limit_is_refused(db, org):
    """Zero would mean an Account that can never spend, which suspend says."""
    keys = FakeKeys()

    with pytest.raises(InvalidRequest):
        await service.create_account(
            db, organization_id=org, name="Zero", limit_usd=Decimal("0"), keys=keys
        )

    assert keys.created == []


async def test_a_new_organization_comes_with_a_default_account(db):
    keys = FakeKeys()

    organization = await org_service.create_organization(
        db, slug="fresh", name="Fresh", keys=keys
    )
    await db.commit()

    default = await accounts_q.get_default_account(
        db, organization_id=organization.id
    )
    assert default is not None
    assert default.is_default is True
    assert default.name == service.DEFAULT_ACCOUNT_NAME
    assert default.status == ACCOUNT_ACTIVE


async def test_an_organization_cannot_hold_two_defaults(db, org):
    """A request naming no Account resolves to the default, so two is a toss-up."""
    keys = FakeKeys()
    await service.create_account(
        db, organization_id=org, name="First", is_default=True, keys=keys
    )

    with pytest.raises(Exception):
        await service.create_account(
            db, organization_id=org, name="Second", is_default=True, keys=keys
        )


def test_a_key_name_cannot_hide_a_separator():
    """The name is a path, so a segment containing one would read as two."""
    with pytest.raises(ValueError):
        service.key_name(
            environment="test",
            organization_id="org:evil",
            account_id="acct_1",
            generation=1,
        )
    with pytest.raises(ValueError):
        service.key_name(
            environment="test",
            organization_id="org_1",
            account_id="acct 1",
            generation=1,
        )


def test_the_key_name_carries_every_part_of_the_identity():
    assert (
        service.key_name(
            environment="Staging",
            organization_id="org_1",
            account_id="acct_2",
            generation=3,
        )
        == "vma:staging:org:org_1:acct:acct_2:key:3"
    )


async def test_the_default_account_cannot_be_suspended(db):
    """Suspending it would stop the whole Organization, quietly.

    A request naming no Account resolves to the default, so this reads as one
    Account's business while being every request's. Stopping an Organization is
    a decision that should have to say so.
    """
    keys = FakeKeys()
    organization = await org_service.create_organization(
        db, slug="guarded", name="Guarded", keys=keys
    )
    await db.commit()
    default = await accounts_q.get_default_account(
        db, organization_id=organization.id
    )

    with pytest.raises(Conflict):
        await service.suspend_account(
            db, organization_id=organization.id, account_id=default.id, keys=keys
        )

    # Nothing was disabled at the provider on the way to being refused.
    assert keys.updates == []
    refreshed = await service.get_account(
        db, organization_id=organization.id, account_id=default.id
    )
    assert refreshed.status == ACCOUNT_ACTIVE


async def test_a_non_default_account_is_still_suspendable(db, org):
    """The guard is about the fallback, not about suspension."""
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Side project", keys=keys
    )

    suspended = await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert suspended.status == ACCOUNT_SUSPENDED

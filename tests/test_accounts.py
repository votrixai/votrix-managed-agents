"""Billing Accounts: what one is, and what it refuses to become."""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models.accounts import (
    ACCOUNT_ACTIVE,
    ACCOUNT_SUSPENDED,
    CREDENTIAL_ACTIVE,
    CREDENTIAL_SUSPENDED,
)
from app.db.queries import accounts as accounts_q
from app.utils.openrouter_management import (
    CreatedOpenRouterKey,
    OpenRouterKeyMetadata,
)
from app.models.errors import (
    AccountUnavailable,
    Conflict,
    InvalidRequest,
    NotFound,
)
from app.services import organizations as organizations_service
from app.services import accounts as service
from tests.conftest import FakeKeys


@pytest.fixture(autouse=True)
def key_name_environment(monkeypatch):
    """Names carry the environment, so these tests fix which one."""
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode().rstrip("=")


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

    minted = keys.secrets[0]
    stored = account.credential.encrypted_key
    assert minted not in stored
    assert "sk-or-v1" not in stored
    # Round-trips under the Organization and key it was sealed with. Those are
    # bound into the ciphertext rather than trusted alongside it, so a token
    # lifted into another row does not open.
    assert (
        service._cipher().decrypt(
            stored, organization_id=org, key_hash=account.credential.key_hash
        )
        == minted
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
    from app.db.queries import organizations as organizations_query

    other = await organizations_query.create_organization(db, slug="other", name="Other")
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

    organization = await organizations_service.create_organization(
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


async def test_an_organization_cannot_hold_two_defaults(db, bare_org):
    """A request naming no Account resolves to the default, so two is a toss-up.

    The API cannot ask for this — `is_default` is not a field on the create
    request, and the only caller that passes it is Organization creation. The
    constraint is here against a bug in our own code, so it fails as one:
    loudly, at the write, before anything is minted.
    """
    keys = FakeKeys()
    await service.create_account(
        db, organization_id=bare_org, name="First", is_default=True, keys=keys
    )

    with pytest.raises(IntegrityError):
        await service.create_account(
            db, organization_id=bare_org, name="Second", is_default=True, keys=keys
        )
    await db.rollback()

    # Refused before the provider was asked for anything, so no key is left
    # behind belonging to an Account that does not exist.
    assert len(keys.created) == 1


async def test_many_non_default_accounts_coexist(db, org):
    """The constraint has to stop a second default without capping the rest."""
    keys = FakeKeys()

    made = [
        await service.create_account(
            db, organization_id=org, name=f"Team {index}", keys=keys
        )
        for index in range(3)
    ]

    assert len({account.id for account in made}) == 3
    assert all(account.is_default is None for account in made)


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
    organization = await organizations_service.create_organization(
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


async def test_ensure_creates_the_default_an_organization_never_got(db, bare_org):
    """Organizations made before Accounts existed have none."""
    keys = FakeKeys()
    assert await accounts_q.get_default_account(db, organization_id=bare_org) is None

    default = await service.ensure_default_account(
        db, organization_id=bare_org, keys=keys
    )

    assert default.is_default is True
    assert default.status == ACCOUNT_ACTIVE
    assert default.name == service.DEFAULT_ACCOUNT_NAME


async def test_ensure_is_safe_to_call_again(db, bare_org):
    keys = FakeKeys()
    first = await service.ensure_default_account(db, organization_id=bare_org, keys=keys)

    second = await service.ensure_default_account(db, organization_id=bare_org, keys=keys)

    assert first.id == second.id
    assert len(keys.created) == 1


async def test_ensure_finishes_an_account_left_without_a_credential(db, bare_org):
    """Creation is two steps and can stop between them.

    What is left is an Organization owning an Account it cannot spend through,
    and no way to make a second default. This is the way out.
    """
    keys = FakeKeys()
    stranded_id = (
        await accounts_q.create_account(
            db, organization_id=bare_org, name=service.DEFAULT_ACCOUNT_NAME, is_default=True
        )
    ).id
    await db.commit()
    # Re-read rather than holding the row: a freshly added instance has no
    # loaded relationship, and touching one is lazy IO in the wrong place.
    stranded = await accounts_q.get_default_account(db, organization_id=bare_org)
    assert stranded.credential is None

    repaired = await service.ensure_default_account(
        db, organization_id=bare_org, keys=keys
    )

    assert repaired.id == stranded_id
    assert repaired.status == ACCOUNT_ACTIVE
    assert repaired.credential is not None
    assert keys.created[0][0].endswith(f":acct:{stranded_id}:key:1")


async def test_ensure_refuses_when_a_key_by_that_name_is_already_out_there(db, bare_org):
    """A mint that succeeded and then failed to record leaves one behind.

    Its secret cannot be read back, so it can be neither adopted nor replaced
    from here. Minting again would leave a live credential nothing accounts
    for, which is worse than stopping.
    """
    keys = FakeKeys()
    stranded_id = (
        await accounts_q.create_account(
            db, organization_id=bare_org, name=service.DEFAULT_ACCOUNT_NAME, is_default=True
        )
    ).id
    await db.commit()
    # Exactly what the interrupted attempt would have left at the provider.
    await keys.create_key(
        name=service.key_name(
            environment="test",
            organization_id=bare_org,
            account_id=stranded_id,
            generation=1,
        )
    )

    with pytest.raises(Conflict):
        await service.ensure_default_account(db, organization_id=bare_org, keys=keys)

    assert len(keys.created) == 1


async def test_a_suspended_account_refuses_to_hand_over_its_key(db, org):
    """The provider would answer a bare 401 that names no reason.

    Refusing here is what turns that into something a caller can act on.
    """
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    with pytest.raises(AccountUnavailable) as raised:
        await service.resolve_spendable_key(
            db, organization_id=org, account_id=account.id
        )

    assert "suspended" in str(raised.value)


async def test_an_active_account_hands_over_the_key_it_was_given(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )

    resolved = await service.resolve_spendable_key(
        db, organization_id=org, account_id=account.id
    )

    assert resolved == keys.secrets[0]


async def test_resuming_makes_the_key_available_again(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )
    await service.resume_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert (
        await service.resolve_spendable_key(
            db, organization_id=org, account_id=account.id
        )
        == keys.secrets[0]
    )


async def test_an_unprovisioned_account_has_no_key_to_hand_over(db, org):
    """Refused for a different reason: there is nothing to decrypt."""
    stranded_id = (
        await accounts_q.create_account(
            db, organization_id=org, name="Half made", is_default=False
        )
    ).id
    await db.commit()

    with pytest.raises(AccountUnavailable) as raised:
        await service.resolve_spendable_key(
            db, organization_id=org, account_id=stranded_id
        )

    assert "provisioned" in str(raised.value)


async def test_usage_comes_from_the_provider_that_charges_it(db, org):
    """Read where the money leaves, not where we happened to be looking.

    A total accumulated from turns we observed is short by whatever we did not
    observe, and short in one direction only.
    """
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    keys.usage[account.credential.key_hash] = Decimal("2.5")

    usage = await service.get_account_usage(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert usage.usage_usd == Decimal("2.5")


async def test_usage_reports_the_headroom_left_under_a_limit(db, org):
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Capped", limit_usd=Decimal("10"), keys=keys
    )
    keys.usage[account.credential.key_hash] = Decimal("4")

    usage = await service.get_account_usage(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert usage.limit_usd == Decimal("10")
    assert usage.limit_remaining_usd == Decimal("6")


async def test_an_uncapped_account_reports_no_ceiling_rather_than_zero(db, org):
    """None is uncapped; zero would read as an Account that may spend nothing."""
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Open", keys=keys
    )

    usage = await service.get_account_usage(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert usage.limit_usd is None
    assert usage.limit_remaining_usd is None


async def test_a_suspended_account_still_reports_what_it_spent(db, org):
    """Most of the reason an Account is suspended rather than removed."""
    keys = FakeKeys()
    account = await service.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    keys.usage[account.credential.key_hash] = Decimal("7.25")
    await service.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    usage = await service.get_account_usage(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    assert usage.usage_usd == Decimal("7.25")


async def test_an_account_with_no_credential_has_nothing_to_report(db, bare_org):
    keys = FakeKeys()
    stranded_id = (
        await accounts_q.create_account(
            db, organization_id=bare_org, name="Half made", is_default=False
        )
    ).id
    await db.commit()

    with pytest.raises(AccountUnavailable):
        await service.get_account_usage(
            db, organization_id=bare_org, account_id=stranded_id, keys=keys
        )

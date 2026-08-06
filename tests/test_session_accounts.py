"""Which Account a Session is billed to, and when that is decided."""

from __future__ import annotations

import pytest

from app.db.queries import organization_accounts as accounts_q
from app.db.queries import sessions as sessions_q
from app.models.errors import AccountUnavailable
from app.services import organization_accounts as billing_accounts
from tests.conftest import FakeKeys


async def test_a_session_is_pinned_to_the_account_it_resolved(
    db, org, agent, environment
):
    """Pinned at creation, so the row says who pays without asking again.

    Resolving per turn instead would move a conversation's spend to a new
    Account the moment the Organization's default changed under it.
    """
    default = await accounts_q.get_default_account(db, organization_id=org)
    resolved = await billing_accounts.require_spendable_account(
        db, organization_id=org
    )

    session = await sessions_q.create_session(
        db,
        organization_id=org,
        agent_id=agent.id,
        agent_version=agent.active_version,
        environment_id=environment.id,
        account_id=resolved.id,
    )
    await db.commit()

    assert session.account_id == default.id


async def test_the_account_is_checked_before_a_session_can_name_it(db, org):
    """A Session opened on a stopped Account would be born unable to run.

    The failure would arrive later, on the first turn, wearing the name of
    something else.
    """
    keys = FakeKeys()
    account = await billing_accounts.create_account(
        db, organization_id=org, name="Stopped", keys=keys
    )
    await billing_accounts.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    with pytest.raises(AccountUnavailable):
        await billing_accounts.require_spendable_account(
            db, organization_id=org, account_id=account.id
        )


async def test_an_organization_with_no_default_cannot_resolve_one(db, bare_org):
    with pytest.raises(AccountUnavailable) as raised:
        await billing_accounts.require_spendable_account(
            db, organization_id=bare_org
        )

    assert "no default Account" in str(raised.value)


async def test_a_session_predating_accounts_falls_back_to_the_default(db, org):
    """Its `account_id` is NULL, and it still has to be able to run.

    Resolving None as the Organization's default is what keeps a conversation
    opened before any of this existed from failing on its next turn.
    """
    default = await accounts_q.get_default_account(db, organization_id=org)

    resolved = await billing_accounts.require_spendable_account(
        db, organization_id=org, account_id=None
    )

    assert resolved.id == default.id


async def test_the_key_a_turn_spends_belongs_to_the_pinned_account(db, org):
    keys = FakeKeys()
    account = await billing_accounts.create_account(
        db, organization_id=org, name="Research", keys=keys
    )

    resolved = await billing_accounts.resolve_spendable_key(
        db, organization_id=org, account_id=account.id
    )

    # The Account's own key, not the default's — one key per Account is the
    # whole reason spend can be told apart.
    assert resolved == keys.secrets[0]
    default_key = await billing_accounts.resolve_spendable_key(
        db, organization_id=org, account_id=None
    )
    assert default_key != resolved


async def test_suspending_stops_the_next_turn_of_a_running_session(db, org):
    """Suspension is meant to bite on the next call, not the next Session.

    The key is resolved per turn for exactly this reason: a Session pinned to
    an Account stopped since it opened has to fail, and fail saying why.
    """
    keys = FakeKeys()
    account = await billing_accounts.create_account(
        db, organization_id=org, name="Research", keys=keys
    )
    assert await billing_accounts.resolve_spendable_key(
        db, organization_id=org, account_id=account.id
    )

    await billing_accounts.suspend_account(
        db, organization_id=org, account_id=account.id, keys=keys
    )

    with pytest.raises(AccountUnavailable):
        await billing_accounts.resolve_spendable_key(
            db, organization_id=org, account_id=account.id
        )

"""Billing Accounts: create one, read them, stop and restart one.

An Account is created in two steps that cannot be one transaction — a row here
and a key at the provider. The row is written first so a mint that fails leaves
something to find, and the Account only reports itself usable once both exist.

There is no delete. An Account holds the record of what it was charged, so
removing one removes that record. Suspension is how one stops spending.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.accounts import (
    ACCOUNT_ACTIVE,
    ACCOUNT_PROVISIONING,
    ACCOUNT_SUSPENDED,
    CREDENTIAL_ACTIVE,
    CREDENTIAL_SUSPENDED,
    OrganizationAccount,
)
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import accounts as accounts_q
from app.utils.openrouter_management import (
    OpenRouterKeyAdmin,
    OpenRouterKeyUsage,
    OpenRouterManagementClient,
)
from app.models.errors import (
    AccountUnavailable,
    Conflict,
    InvalidRequest,
    NotFound,
)
from app.utils.secret_cipher import SecretCipher

DEFAULT_ACCOUNT_NAME = "Default"

# The name is a colon-delimited path, so no segment may contain one. Whitespace
# is out for the same reason a provider console is: a name that looks like two
# names is unreadable as attribution.
_KEY_NAME_FORBIDDEN = re.compile(r"[:\s]")


def key_name(
    *,
    environment: str,
    organization_id: str,
    account_id: str,
    generation: int,
) -> str:
    """The provider-visible name identifying one Account's credential.

    The Account id is in the name because the name is the only attribution
    readable from the provider's side: a console or a billing export shows it,
    while the credential-to-Account mapping otherwise exists solely in this
    database. Without it a key found there cannot be traced to what it bills.

    The environment is first so that staging and production keys sharing one
    provider workspace stay tellable apart, and the generation is last so a
    rotated key is distinguishable from the one it replaced.
    """
    env = environment.strip().lower()
    for value, label in (
        (env, "environment"),
        (organization_id, "organization_id"),
        (account_id, "account_id"),
    ):
        if not value or _KEY_NAME_FORBIDDEN.search(value):
            raise ValueError(f"{label} cannot be encoded in a provider key name")
    if generation < 1:
        raise ValueError("generation must be positive")
    return f"vma:{env}:org:{organization_id}:acct:{account_id}:key:{generation}"


async def create_account(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    is_default: bool = False,
    limit_usd: Decimal | None = None,
    idempotency_key: str | None = None,
    keys: OpenRouterKeyAdmin | None = None,
) -> OrganizationAccount:
    """Create one Account and the credential it spends through.

    Idempotent on `idempotency_key` within the Organization: a repeat returns
    the Account the first call made. Without that, a client retrying an
    ambiguous response gets a second Account billing the same thing, and the
    first one's key is left spending with nobody watching it.
    """
    display_name = name.strip()
    if not display_name:
        raise InvalidRequest("Account name cannot be empty")
    if limit_usd is not None and limit_usd <= 0:
        raise InvalidRequest("Account limit must be greater than zero")

    if idempotency_key:
        existing = await accounts_q.get_by_idempotency_key(
            db, organization_id=organization_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.status == ACCOUNT_PROVISIONING:
                # The first attempt did not finish. Returning it would hand
                # back an Account nothing can spend on, and minting a second
                # key for it would leave the first one unattributed.
                raise Conflict(
                    "The original Account create did not complete; retry with a new key"
                )
            return existing

    account = await accounts_q.create_account(
        db,
        organization_id=organization_id,
        name=display_name,
        is_default=is_default,
        limit_usd=limit_usd,
        idempotency_key=idempotency_key,
    )
    await db.commit()

    return await _provision_credential(db, account=account, keys=keys or _key_admin())


async def _provision_credential(
    db: AsyncSession,
    *,
    account: OrganizationAccount,
    keys: OpenRouterKeyAdmin,
) -> OrganizationAccount:
    """Mint the Account's key and record it, which is what makes it usable."""
    created = await keys.create_key(
        name=key_name(
            environment=get_settings().app_env,
            organization_id=account.organization_id,
            account_id=account.id,
            generation=1,
        ),
        limit_usd=account.limit_usd,
    )

    await accounts_q.attach_credential(
        db,
        account=account,
        key_hash=created.key_hash,
        provider_key_name=created.key_name,
        encrypted_key=_cipher().encrypt(
            created.secret.get_secret_value(),
            organization_id=account.organization_id,
            key_hash=created.key_hash,
        ),
    )
    account.status = ACCOUNT_ACTIVE
    await db.commit()
    await db.refresh(account)
    return account


async def ensure_default_account(
    db: AsyncSession,
    *,
    organization_id: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> OrganizationAccount:
    """Make sure this Organization has a default Account, and return it.

    Creating one is two steps that cannot be one transaction, so it can stop
    between them and leave an Organization that owns nothing it can spend
    through. This is what finishes the job — and what gives an Organization
    made before Accounts existed the one it never got.

    Safe to call repeatedly: an Account already in place is returned untouched.
    """
    default = await accounts_q.get_default_account(
        db, organization_id=organization_id
    )
    if default is None:
        return await create_default_account(
            db, organization_id=organization_id, keys=keys
        )
    if default.credential is not None:
        # Whatever state it is in, it has something to spend through, and
        # changing that is a decision this function does not get to make.
        return default

    provider = keys or _key_admin()
    expected = key_name(
        environment=get_settings().app_env,
        organization_id=organization_id,
        account_id=default.id,
        generation=1,
    )
    if any(key.key_name == expected for key in await provider.list_keys()):
        # A key by this name is already out there, from an attempt that minted
        # one and failed before recording it. Its secret cannot be read back,
        # so it can neither be adopted nor safely replaced from here — minting
        # again would leave a live credential nothing accounts for.
        #
        # The name stays out of the message. It spells out the provider, the
        # environment and our key-naming scheme, and this error is one an
        # Account holder could be shown. An operator finds the key by building
        # the same name from the Account id, which the message does give.
        raise Conflict(
            f"Account {default.id} already has a provider key from an "
            "interrupted attempt; it has to be revoked before this Account "
            "can be provisioned"
        )
    return await _provision_credential(db, account=default, keys=provider)


async def get_account_usage(
    db: AsyncSession,
    *,
    organization_id: str,
    account_id: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> OpenRouterKeyUsage:
    """What this Account has spent, as the provider counts it.

    Read from the credential's own counters rather than accumulated from turns
    we watched. Anything spent on this Account's key is in the answer — a
    sub-agent, a retry, a path nobody instrumented — because the figure is
    measured where the money leaves rather than where we happened to look.

    Available while suspended, which is most of the reason an Account is
    suspended instead of removed: what it spent has to stay readable.

    The lifetime figure belongs to the credential. Nothing rotates one today,
    so it is also the Account's lifetime; when something does, the number will
    start again and the rest of this will need to carry the old one forward.
    """
    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account.credential is None:
        raise AccountUnavailable(
            f"Account {account_id} has no credential, so it has spent nothing "
            "and there is nothing to report"
        )
    key_hash = account.credential.key_hash
    # Released before the provider call: this read is not worth holding a
    # connection across a network round trip.
    await db.rollback()
    return await (keys or _key_admin()).get_key_usage(key_hash)


async def require_spendable_account(
    db: AsyncSession, *, organization_id: str, account_id: str | None = None
) -> OrganizationAccount:
    """The Account a request will be billed to, if it is in a state to be.

    Without an id this is the Organization's default, which is what a request
    that names no Account means.

    Checked when a Session opens as well as when its key is fetched: opening
    one on a suspended Account would create a conversation born unable to run,
    and the failure would arrive later wearing a different name.
    """
    if account_id is None:
        account = await accounts_q.get_default_account(
            db, organization_id=organization_id
        )
        if account is None:
            raise AccountUnavailable(
                f"Organization {organization_id} has no default Account"
            )
    else:
        account = await get_account(
            db, organization_id=organization_id, account_id=account_id
        )

    if account.status == ACCOUNT_SUSPENDED:
        raise AccountUnavailable(
            f"Account {account.id} is suspended and cannot be spent through; "
            "resume it first"
        )
    if account.status != ACCOUNT_ACTIVE or account.credential is None:
        raise AccountUnavailable(
            f"Account {account.id} has no usable credential; it was never "
            "finished being provisioned"
        )
    if account.credential.status != CREDENTIAL_ACTIVE:
        # The pair disagreeing means something moved one without the other.
        # Spending on it would be spending on a credential nothing believes is
        # live, so this stops rather than picking a side.
        raise AccountUnavailable(
            f"Account {account.id} and its credential disagree about being active"
        )
    return account


async def resolve_spendable_key(
    db: AsyncSession, *, organization_id: str, account_id: str | None = None
) -> str:
    """The plaintext key this Account spends through, if it still may.

    The only place a stored credential is decrypted. The check runs again here
    rather than trusting the one at Session creation, because an Account can be
    suspended in between — and the whole point of suspending is that it takes
    effect on the next call, not the next Session.
    """
    account = await require_spendable_account(
        db, organization_id=organization_id, account_id=account_id
    )
    return _cipher().decrypt(
        account.credential.encrypted_key,
        organization_id=organization_id,
        key_hash=account.credential.key_hash,
    )


async def get_account(
    db: AsyncSession, *, organization_id: str, account_id: str
) -> OrganizationAccount:
    account = await accounts_q.get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account is None:
        raise NotFound(f"Account {account_id} not found")
    return account


async def list_accounts(
    db: AsyncSession,
    *,
    organization_id: str,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    return await accounts_q.list_accounts(
        db,
        organization_id=organization_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def suspend_account(
    db: AsyncSession,
    *,
    organization_id: str,
    account_id: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> OrganizationAccount:
    """Stop an Account spending, and keep everything recorded against it.

    The key is disabled at the provider rather than deleted, so what it spent
    stays attributable and the same credential can be turned back on. Sessions
    already running on it will fail their next call — accepted, because the
    point of suspending is that spending stops now.
    """
    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account.is_default:
        # A request naming no Account resolves here, so suspending it stops the
        # whole Organization while reading as one Account's business. Stopping
        # an Organization is a decision that should have to say so.
        raise Conflict(
            f"Account {account_id} is this Organization's default and cannot be suspended"
        )
    if account.status == ACCOUNT_SUSPENDED:
        return account
    if account.status != ACCOUNT_ACTIVE or account.credential is None:
        raise Conflict(f"Account {account_id} is not active")

    key_hash = account.credential.key_hash
    await db.rollback()
    await (keys or _key_admin()).update_key(key_hash, disabled=True)

    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    await accounts_q.set_status(
        db,
        account=account,
        account_status=ACCOUNT_SUSPENDED,
        credential_status=CREDENTIAL_SUSPENDED,
    )
    await db.commit()
    return account


async def resume_account(
    db: AsyncSession,
    *,
    organization_id: str,
    account_id: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> OrganizationAccount:
    """Let a suspended Account spend again on the credential it already had."""
    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account.status == ACCOUNT_ACTIVE:
        return account
    if account.status != ACCOUNT_SUSPENDED or account.credential is None:
        raise Conflict(f"Account {account_id} is not suspended")

    key_hash = account.credential.key_hash
    await db.rollback()
    await (keys or _key_admin()).update_key(key_hash, disabled=False)

    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    await accounts_q.set_status(
        db,
        account=account,
        account_status=ACCOUNT_ACTIVE,
        credential_status=CREDENTIAL_ACTIVE,
    )
    await db.commit()
    return account


async def create_default_account(
    db: AsyncSession,
    *,
    organization_id: str,
    keys: OpenRouterKeyAdmin | None = None,
) -> OrganizationAccount:
    """The Account an Organization has before anyone asks for one.

    A request that names no Account resolves here, so an Organization without
    one has nothing to spend through at all.
    """
    return await create_account(
        db,
        organization_id=organization_id,
        name=DEFAULT_ACCOUNT_NAME,
        is_default=True,
        keys=keys,
    )


@lru_cache(maxsize=1)
def _key_admin() -> OpenRouterManagementClient:
    return OpenRouterManagementClient(get_settings().openrouter_management_key)


@lru_cache(maxsize=1)
def _cipher() -> SecretCipher:
    return SecretCipher.from_base64(get_settings().vma_encryption_key)


__all__ = [
    "DEFAULT_ACCOUNT_NAME",
    "create_account",
    "create_default_account",
    "ensure_default_account",
    "get_account",
    "get_account_usage",
    "key_name",
    "list_accounts",
    "require_spendable_account",
    "resolve_spendable_key",
    "resume_account",
    "suspend_account",
]

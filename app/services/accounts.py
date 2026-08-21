"""Billing Accounts: create one, read them, stop and restart one.

A Platform Account is created in two steps that cannot be one transaction — a
row here and a managed key at OpenRouter. A BYOK Account instead validates the
user-owned key first, then records its row and encrypted credential atomically.
Neither reports itself usable until its credential is in place.

There is no delete. An Account holds the record of what it was charged, so
removing one removes that record. Suspension is how one stops spending.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.accounts import (
    ACCOUNT_ACTIVE,
    ACCOUNT_PROVISIONING,
    ACCOUNT_SUSPENDED,
    CREDENTIAL_ACTIVE,
    CREDENTIAL_OPENROUTER,
    CREDENTIAL_SUSPENDED,
    DIRECT_CREDENTIAL_PROVIDERS,
    FUNDING_BYOK,
    FUNDING_MODES,
    FUNDING_PLATFORM,
    AccountModelCredential,
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
from app.services.account_credentials import (
    ByokKeyValidator,
    HttpByokKeyValidator,
    ResolvedAccountCredential,
    SubmittedByokCredential,
    credential_fingerprint,
)
from app.utils.secret_cipher import SecretCipher

DEFAULT_ACCOUNT_NAME = "Default"


@dataclass(frozen=True, slots=True)
class AccountUsageSnapshot:
    funding_mode: str
    backends: tuple[str, ...]
    usage_usd: Decimal | None
    usage_daily_usd: Decimal | None
    usage_weekly_usd: Decimal | None
    usage_monthly_usd: Decimal | None
    limit_usd: Decimal | None
    limit_remaining_usd: Decimal | None
    observed_input_tokens: int
    observed_output_tokens: int
    observed_total_tokens: int


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


def _normalize_byok_credentials(
    credentials: Sequence[SubmittedByokCredential],
) -> tuple[SubmittedByokCredential, ...]:
    if not credentials:
        raise InvalidRequest("BYOK Accounts require at least one direct credential")
    if len(credentials) > len(DIRECT_CREDENTIAL_PROVIDERS):
        raise InvalidRequest("Too many BYOK credentials")

    normalized: list[SubmittedByokCredential] = []
    seen: set[str] = set()
    for credential in credentials:
        if credential.backend not in DIRECT_CREDENTIAL_PROVIDERS:
            raise InvalidRequest(
                f"Unsupported direct BYOK backend {credential.backend!r}"
            )
        if credential.backend in seen:
            raise InvalidRequest(
                f"BYOK Account already has a {credential.backend} credential"
            )
        plaintext = credential.api_key.get_secret_value().strip()
        if not plaintext:
            raise InvalidRequest(
                f"BYOK {credential.backend} credential requires an api_key"
            )
        seen.add(credential.backend)
        normalized.append(
            SubmittedByokCredential(
                backend=credential.backend,
                api_key=SecretStr(plaintext),
            )
        )
    return tuple(normalized)


def _credential_for_backend(
    account: OrganizationAccount,
    backend: str,
) -> AccountModelCredential | None:
    return next(
        (
            credential
            for credential in account.model_credentials
            if credential.backend == backend
        ),
        None,
    )


async def create_account(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    is_default: bool = False,
    limit_usd: Decimal | None = None,
    idempotency_key: str | None = None,
    funding_mode: str = FUNDING_PLATFORM,
    byok_credentials: Sequence[SubmittedByokCredential] | None = None,
    keys: OpenRouterKeyAdmin | None = None,
    byok_validator: ByokKeyValidator | None = None,
) -> OrganizationAccount:
    """Create one Platform credential or a set of direct BYOK credentials.

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
    if funding_mode not in FUNDING_MODES:
        raise InvalidRequest(f"Unsupported Account funding mode {funding_mode!r}")
    if funding_mode == FUNDING_PLATFORM:
        if byok_credentials:
            raise InvalidRequest(
                "Platform-funded Accounts do not accept BYOK credentials"
            )
        normalized_byok: tuple[SubmittedByokCredential, ...] = ()
    else:
        if limit_usd is not None:
            raise InvalidRequest(
                "Account limit is only available for Platform-funded Accounts"
            )
        normalized_byok = _normalize_byok_credentials(byok_credentials or ())

    if idempotency_key:
        existing = await accounts_q.get_by_idempotency_key(
            db, organization_id=organization_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.funding_mode != funding_mode:
                raise Conflict(
                    "The idempotency key already belongs to an Account with "
                    "different funding"
                )
            if funding_mode == FUNDING_BYOK:
                repeated = {
                    credential.backend: credential_fingerprint(
                        backend=credential.backend,
                        api_key=credential.api_key,
                    )
                    for credential in normalized_byok
                }
                recorded = {
                    credential.backend: credential.key_hash
                    for credential in existing.model_credentials
                }
                if recorded != repeated:
                    raise Conflict(
                        "The idempotency key already belongs to an Account with "
                        "different BYOK credentials"
                    )
            if existing.status == ACCOUNT_PROVISIONING:
                # The first attempt did not finish. Returning it would hand
                # back an Account nothing can spend on, and minting a second
                # key for it would leave the first one unattributed.
                raise Conflict(
                    "The original Account create did not complete; retry with a new key"
                )
            return existing

    if funding_mode == FUNDING_BYOK:
        # Do not hold a database transaction while waiting on a third party.
        # The unique idempotency and fingerprint constraints settle a race
        # safely if another request creates the same Account in the meantime.
        await db.rollback()
        validator = byok_validator or _byok_key_validator()
        await asyncio.gather(
            *(
                validator.validate(
                    backend=credential.backend,
                    api_key=credential.api_key,
                )
                for credential in normalized_byok
            )
        )
        try:
            account = await accounts_q.create_account(
                db,
                organization_id=organization_id,
                name=display_name,
                is_default=is_default,
                limit_usd=None,
                idempotency_key=idempotency_key,
                funding_mode=FUNDING_BYOK,
            )
            for credential in normalized_byok:
                fingerprint = credential_fingerprint(
                    backend=credential.backend,
                    api_key=credential.api_key,
                )
                await accounts_q.attach_credential(
                    db,
                    account=account,
                    backend=credential.backend,
                    key_hash=fingerprint,
                    provider_key_name=None,
                    encrypted_key=_cipher().encrypt(
                        credential.api_key.get_secret_value(),
                        organization_id=organization_id,
                        key_hash=fingerprint,
                    ),
                )
            account.status = ACCOUNT_ACTIVE
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise Conflict(
                "That BYOK key or idempotency key is already attached to an Account"
            ) from exc
        await db.refresh(account, attribute_names=["model_credentials"])
        return account

    account = await accounts_q.create_account(
        db,
        organization_id=organization_id,
        name=display_name,
        is_default=is_default,
        limit_usd=limit_usd,
        idempotency_key=idempotency_key,
        funding_mode=FUNDING_PLATFORM,
    )
    await db.commit()

    return await _provision_credential(db, account=account, keys=keys or _key_admin())


def _require_byok_account(account: OrganizationAccount) -> None:
    if account.funding_mode != FUNDING_BYOK:
        raise Conflict(
            f"Platform Account {account.id} does not accept user-owned credentials"
        )


def _credential_status_for_account(account: OrganizationAccount) -> str:
    if account.status == ACCOUNT_ACTIVE:
        return CREDENTIAL_ACTIVE
    if account.status == ACCOUNT_SUSPENDED:
        return CREDENTIAL_SUSPENDED
    raise Conflict(
        f"Account {account.id} is still provisioning and cannot change credentials"
    )


async def set_byok_model_credential(
    db: AsyncSession,
    *,
    organization_id: str,
    account_id: str,
    backend: str,
    api_key: SecretStr,
    byok_validator: ByokKeyValidator | None = None,
) -> OrganizationAccount:
    """Add or atomically replace one direct backend key on a BYOK Account."""

    if backend not in DIRECT_CREDENTIAL_PROVIDERS:
        raise InvalidRequest(f"Unsupported direct BYOK backend {backend!r}")
    plaintext = api_key.get_secret_value().strip()
    if not plaintext:
        raise InvalidRequest(f"BYOK {backend} credential requires an api_key")
    submitted = SecretStr(plaintext)
    fingerprint = credential_fingerprint(backend=backend, api_key=submitted)

    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    _require_byok_account(account)
    existing = _credential_for_backend(account, backend)
    if existing is not None and existing.key_hash == fingerprint:
        # Even an idempotent PUT participates in the same parent-row lock as a
        # concurrent DELETE. Whichever mutation obtains the lock last wins,
        # rather than this request reporting success from a stale pre-lock read.
        await db.rollback()
        account = await accounts_q.get_account_for_update(
            db, organization_id=organization_id, account_id=account_id
        )
        if account is None:
            raise NotFound(f"Account {account_id} not found")
        _require_byok_account(account)
        existing = _credential_for_backend(account, backend)
        if existing is not None and existing.key_hash == fingerprint:
            await db.commit()
            return account

    # Keep the Account row unlocked while the provider validates the key. The
    # row is locked only for the short mutation below; after acquiring it we
    # re-read every decision that could have changed during the network call.
    await db.rollback()
    await (byok_validator or _byok_key_validator()).validate(
        backend=backend,
        api_key=submitted,
    )
    encrypted_key = _cipher().encrypt(
        plaintext,
        organization_id=organization_id,
        key_hash=fingerprint,
    )

    try:
        account = await accounts_q.get_account_for_update(
            db, organization_id=organization_id, account_id=account_id
        )
        if account is None:
            raise NotFound(f"Account {account_id} not found")
        _require_byok_account(account)
        credential_status = _credential_status_for_account(account)
        existing = _credential_for_backend(account, backend)
        if existing is not None and existing.key_hash == fingerprint:
            # Another identical PUT won the race while validation was in
            # flight. Commit the read-only transaction to release the lock and
            # return the state it established.
            await db.commit()
            return account
        if existing is None:
            await accounts_q.attach_credential(
                db,
                account=account,
                backend=backend,
                key_hash=fingerprint,
                provider_key_name=None,
                encrypted_key=encrypted_key,
                status=credential_status,
            )
        else:
            await accounts_q.replace_credential(
                db,
                credential=existing,
                key_hash=fingerprint,
                encrypted_key=encrypted_key,
                status=credential_status,
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        current = await accounts_q.get_account(
            db, organization_id=organization_id, account_id=account_id
        )
        if current is not None:
            recorded = _credential_for_backend(current, backend)
            if recorded is not None and recorded.key_hash == fingerprint:
                return current
        raise Conflict(
            "That BYOK key is already attached to another Account"
        ) from exc

    await db.refresh(account, attribute_names=["model_credentials"])
    return account


async def delete_byok_model_credential(
    db: AsyncSession,
    *,
    organization_id: str,
    account_id: str,
    backend: str,
) -> OrganizationAccount:
    """Remove one direct key while keeping every BYOK Account usable."""

    if backend not in DIRECT_CREDENTIAL_PROVIDERS:
        raise InvalidRequest(f"Unsupported direct BYOK backend {backend!r}")
    account = await accounts_q.get_account_for_update(
        db, organization_id=organization_id, account_id=account_id
    )
    if account is None:
        raise NotFound(f"Account {account_id} not found")
    _require_byok_account(account)
    credential = _credential_for_backend(account, backend)
    if credential is None:
        raise NotFound(
            f"BYOK Account {account_id} has no {backend} credential"
        )
    if len(account.model_credentials) == 1:
        raise Conflict(
            "A BYOK Account must keep at least one model credential"
        )

    await accounts_q.delete_credential(db, credential=credential)
    await db.commit()
    await db.refresh(account, attribute_names=["model_credentials"])
    return account


async def _provision_credential(
    db: AsyncSession,
    *,
    account: OrganizationAccount,
    keys: OpenRouterKeyAdmin,
) -> OrganizationAccount:
    """Mint the Account's key and record it, which is what makes it usable."""
    if account.funding_mode != FUNDING_PLATFORM:
        raise Conflict(
            f"BYOK Account {account.id} cannot be provisioned with a Platform key"
        )
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
        backend=CREDENTIAL_OPENROUTER,
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
    await db.refresh(account, attribute_names=["model_credentials"])
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
    if default.model_credentials:
        # Whatever state it is in, it has something to spend through, and
        # changing that is a decision this function does not get to make.
        return default
    if default.funding_mode != FUNDING_PLATFORM:
        raise Conflict(
            f"BYOK Account {default.id} has no credentials and cannot be "
            "provisioned as a Platform Account"
        )

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
) -> AccountUsageSnapshot:
    """External Platform billing and normalized usage observed for an Account.

    Platform USD figures come from the isolated managed key, so they include
    anything charged to it even if VMA did not record the model call. BYOK has
    no common Account-scoped billing API, so its USD fields stay null. Token
    figures for both modes come from completed model calls recorded on Sessions
    pinned to this Account.

    Available while suspended, which is most of the reason an Account is
    suspended instead of removed: what it spent has to stay readable.

    The Platform lifetime figure belongs to its credential. Nothing rotates
    one today, so it is also the Account's lifetime; rotation will need to
    carry the old credential's figure forward.
    """
    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if not account.model_credentials:
        raise AccountUnavailable(
            f"Account {account_id} has no credential, so it has spent nothing "
            "and there is nothing to report"
        )
    observed = await accounts_q.get_observed_token_usage(db, account=account)

    if account.funding_mode == FUNDING_BYOK:
        return AccountUsageSnapshot(
            funding_mode=account.funding_mode,
            backends=tuple(
                credential.backend for credential in account.model_credentials
            ),
            usage_usd=None,
            usage_daily_usd=None,
            usage_weekly_usd=None,
            usage_monthly_usd=None,
            limit_usd=None,
            limit_remaining_usd=None,
            observed_input_tokens=observed[0],
            observed_output_tokens=observed[1],
            observed_total_tokens=observed[2],
        )

    credential = _credential_for_backend(account, CREDENTIAL_OPENROUTER)
    if credential is None:
        raise AccountUnavailable(
            f"Platform Account {account_id} has no OpenRouter credential"
        )
    key_hash = credential.key_hash
    funding_mode = account.funding_mode
    # Released before the provider call: this read is not worth holding a
    # connection across a network round trip.
    await db.rollback()
    provider_usage: OpenRouterKeyUsage = await (keys or _key_admin()).get_key_usage(
        key_hash
    )
    return AccountUsageSnapshot(
        funding_mode=funding_mode,
        backends=(CREDENTIAL_OPENROUTER,),
        usage_usd=provider_usage.usage_usd,
        usage_daily_usd=provider_usage.usage_daily_usd,
        usage_weekly_usd=provider_usage.usage_weekly_usd,
        usage_monthly_usd=provider_usage.usage_monthly_usd,
        limit_usd=provider_usage.limit_usd,
        limit_remaining_usd=provider_usage.limit_remaining_usd,
        observed_input_tokens=observed[0],
        observed_output_tokens=observed[1],
        observed_total_tokens=observed[2],
    )


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
    if account.status != ACCOUNT_ACTIVE or not account.model_credentials:
        raise AccountUnavailable(
            f"Account {account.id} has no usable credential; it was never "
            "finished being provisioned"
        )
    if any(
        credential.status != CREDENTIAL_ACTIVE
        for credential in account.model_credentials
    ):
        # The pair disagreeing means something moved one without the other.
        # Spending on it would be spending on a credential nothing believes is
        # live, so this stops rather than picking a side.
        raise AccountUnavailable(
            f"Account {account.id} and its credential disagree about being active"
        )
    return account


def backend_for_model(
    account: OrganizationAccount,
    *,
    model_provider: str,
) -> str:
    """Choose the only backend this Account may use for one catalog model.

    Platform Accounts deliberately collapse every model provider to their
    managed OpenRouter credential. BYOK Accounts do the opposite: the model's
    provider must have an exact active credential on the same Account. There
    is no fallback across providers or Accounts.
    """

    if account.funding_mode == FUNDING_PLATFORM:
        credential = _credential_for_backend(account, CREDENTIAL_OPENROUTER)
        if credential is None or credential.status != CREDENTIAL_ACTIVE:
            raise AccountUnavailable(
                f"Platform Account {account.id} has no active OpenRouter credential"
            )
        return CREDENTIAL_OPENROUTER

    if model_provider not in DIRECT_CREDENTIAL_PROVIDERS:
        raise AccountUnavailable(
            f"Model provider {model_provider!r} is not supported for direct BYOK"
        )
    credential = _credential_for_backend(account, model_provider)
    if credential is None:
        raise AccountUnavailable(
            f"BYOK Account {account.id} has no {model_provider} credential for "
            "the selected model"
        )
    if credential.status != CREDENTIAL_ACTIVE:
        raise AccountUnavailable(
            f"BYOK Account {account.id} has no active {model_provider} credential"
        )
    return model_provider


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
    if account.funding_mode != FUNDING_PLATFORM:
        raise AccountUnavailable(
            "A BYOK key must be resolved for the selected model provider"
        )
    credential = _credential_for_backend(account, CREDENTIAL_OPENROUTER)
    if credential is None:
        raise AccountUnavailable(
            f"Platform Account {account.id} has no OpenRouter credential"
        )
    return _cipher().decrypt(
        credential.encrypted_key,
        organization_id=organization_id,
        key_hash=credential.key_hash,
    )


async def resolve_spendable_credential(
    db: AsyncSession,
    *,
    organization_id: str,
    model_provider: str,
    account_id: str | None = None,
) -> ResolvedAccountCredential:
    """The Account's decrypted key together with its required backend.

    Runtime callers must route on this object. Returning a bare key made it
    possible to accidentally send a native Anthropic key to OpenRouter.
    """

    account = await require_spendable_account(
        db, organization_id=organization_id, account_id=account_id
    )
    backend = backend_for_model(account, model_provider=model_provider)
    credential = _credential_for_backend(account, backend)
    if credential is None:  # Kept local even if invariants change later.
        raise AccountUnavailable(
            f"Account {account.id} has no credential for backend {backend}"
        )
    plaintext = _cipher().decrypt(
        credential.encrypted_key,
        organization_id=organization_id,
        key_hash=credential.key_hash,
    )
    return ResolvedAccountCredential(
        account_id=account.id,
        funding_mode=account.funding_mode,
        backend=backend,
        api_key=SecretStr(plaintext),
    )


async def resolve_optional_spendable_credential(
    db: AsyncSession,
    *,
    organization_id: str,
    model_provider: str,
    account_id: str | None = None,
) -> ResolvedAccountCredential | None:
    """Resolve an auxiliary model key if the same Account contains one.

    Some tools make their own model call. Missing an optional direct key makes
    that tool unavailable; it must never borrow a key from another Account or
    silently charge the Platform. Platform Accounts reuse their one managed
    OpenRouter key because all of their calls already share that boundary.
    """

    account = await require_spendable_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account.funding_mode == FUNDING_PLATFORM:
        backend = CREDENTIAL_OPENROUTER
    else:
        if model_provider not in DIRECT_CREDENTIAL_PROVIDERS:
            return None
        backend = model_provider

    credential = _credential_for_backend(account, backend)
    if credential is None:
        return None
    plaintext = _cipher().decrypt(
        credential.encrypted_key,
        organization_id=organization_id,
        key_hash=credential.key_hash,
    )
    return ResolvedAccountCredential(
        account_id=account.id,
        funding_mode=account.funding_mode,
        backend=backend,
        api_key=SecretStr(plaintext),
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

    A Platform key is disabled at OpenRouter; a BYOK key remains user-owned and
    is blocked locally. Sessions already running on either fail their next call
    — accepted, because the point of suspending is that VMA spending stops now.
    """
    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account.is_default:
        # A request naming no Account resolves here, so suspending it stops the
        # whole Organization while reading as one Account's business. Stopping
        # an Organization is a decision that should have to say so.
        raise Conflict(
            f"Account {account_id} is this Organization's default and cannot "
            "be suspended"
        )
    if account.status == ACCOUNT_SUSPENDED:
        return account
    if account.status != ACCOUNT_ACTIVE or not account.model_credentials:
        raise Conflict(f"Account {account_id} is not active")

    if account.funding_mode == FUNDING_PLATFORM:
        credential = _credential_for_backend(account, CREDENTIAL_OPENROUTER)
        if credential is None:
            raise Conflict(f"Platform Account {account_id} has no credential")
        key_hash = credential.key_hash
        await db.rollback()
        await (keys or _key_admin()).update_key(key_hash, disabled=True)

    account = await accounts_q.get_account_for_update(
        db, organization_id=organization_id, account_id=account_id
    )
    if account is None:
        raise NotFound(f"Account {account_id} not found")
    if account.status == ACCOUNT_SUSPENDED:
        await db.commit()
        return account
    if account.status != ACCOUNT_ACTIVE or not account.model_credentials:
        raise Conflict(f"Account {account_id} is not active")
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
    byok_validator: ByokKeyValidator | None = None,
) -> OrganizationAccount:
    """Let a suspended Account spend again on the credential it already had."""
    account = await get_account(
        db, organization_id=organization_id, account_id=account_id
    )
    if account.status == ACCOUNT_ACTIVE:
        return account
    if account.status != ACCOUNT_SUSPENDED or not account.model_credentials:
        raise Conflict(f"Account {account_id} is not suspended")

    if account.funding_mode == FUNDING_PLATFORM:
        credential = _credential_for_backend(account, CREDENTIAL_OPENROUTER)
        if credential is None:
            raise Conflict(f"Platform Account {account_id} has no credential")
        key_hash = credential.key_hash
        await db.rollback()
        await (keys or _key_admin()).update_key(key_hash, disabled=False)
    else:
        submitted = tuple(
            SubmittedByokCredential(
                backend=credential.backend,
                api_key=SecretStr(
                    _cipher().decrypt(
                        credential.encrypted_key,
                        organization_id=organization_id,
                        key_hash=credential.key_hash,
                    )
                ),
            )
            for credential in account.model_credentials
        )
        await db.rollback()
        validator = byok_validator or _byok_key_validator()
        await asyncio.gather(
            *(
                validator.validate(
                    backend=credential.backend,
                    api_key=credential.api_key,
                )
                for credential in submitted
            )
        )

    account = await accounts_q.get_account_for_update(
        db, organization_id=organization_id, account_id=account_id
    )
    if account is None:
        raise NotFound(f"Account {account_id} not found")
    if account.status == ACCOUNT_ACTIVE:
        await db.commit()
        return account
    if account.status != ACCOUNT_SUSPENDED or not account.model_credentials:
        raise Conflict(f"Account {account_id} is not suspended")
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


@lru_cache(maxsize=1)
def _byok_key_validator() -> HttpByokKeyValidator:
    return HttpByokKeyValidator()


__all__ = [
    "DEFAULT_ACCOUNT_NAME",
    "AccountUsageSnapshot",
    "backend_for_model",
    "create_account",
    "create_default_account",
    "delete_byok_model_credential",
    "ensure_default_account",
    "get_account",
    "get_account_usage",
    "key_name",
    "list_accounts",
    "require_spendable_account",
    "resolve_spendable_credential",
    "resolve_optional_spendable_credential",
    "resolve_spendable_key",
    "resume_account",
    "set_byok_model_credential",
    "suspend_account",
]

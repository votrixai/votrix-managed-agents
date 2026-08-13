from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VMA_API_SCOPE, VmaApiKey
from app.utils.id_generator import new_id

logger = structlog.get_logger()


VMA_API_KEYS_MANAGE_SCOPE = "api_keys:manage"
VMA_WORKER_SCOPE = "worker"
KNOWN_VMA_API_KEY_SCOPES = frozenset(
    {
        VMA_API_SCOPE,
        VMA_API_KEYS_MANAGE_SCOPE,
        VMA_WORKER_SCOPE,
    }
)
DEFAULT_VMA_API_KEY_SCOPES = (VMA_API_SCOPE,)

VMA_API_KEY_PREFIX = "vma_"
LEGACY_VMA_API_KEY_PREFIXES = (
    "vma_live_",
    "vma_test_",
)
DISPLAYED_VMA_API_KEY_PREFIX_LENGTH = 17
MINIMUM_VMA_API_KEY_RANDOM_LENGTH = 32


def vma_api_key_prefix(*, app_env: str | None = None) -> str:
    """Return the environment-independent prefix for newly issued keys.

    ``app_env`` remains accepted so callers compiled against the former
    environment-specific API do not break during the format migration.
    """
    return VMA_API_KEY_PREFIX


def generate_vma_api_key(*, app_env: str | None = None) -> str:
    prefix = vma_api_key_prefix(app_env=app_env)
    while True:
        token = f"{prefix}{secrets.token_urlsafe(32)}"
        if not token.startswith(LEGACY_VMA_API_KEY_PREFIXES):
            return token


def validate_vma_api_key_prefix(token: str, *, app_env: str | None = None) -> None:
    """Accept unified keys plus the two retired environment-prefixed forms."""
    expected_prefix = vma_api_key_prefix(app_env=app_env)
    if not token.startswith(expected_prefix):
        raise ValueError(f"api_key must use the {expected_prefix} prefix")
    entropy_prefix = next(
        (
            prefix
            for prefix in LEGACY_VMA_API_KEY_PREFIXES
            if token.startswith(prefix)
        ),
        expected_prefix,
    )
    if len(token) < len(entropy_prefix) + MINIMUM_VMA_API_KEY_RANDOM_LENGTH:
        raise ValueError(
            "api_key must contain at least "
            f"{MINIMUM_VMA_API_KEY_RANDOM_LENGTH} characters after the prefix"
        )


def is_legacy_vma_api_key(token: str) -> bool:
    return token.startswith(LEGACY_VMA_API_KEY_PREFIXES)


def validate_legacy_vma_api_key(token: str) -> None:
    if not is_legacy_vma_api_key(token):
        raise ValueError("legacy api_key must use the vma_live_ or vma_test_ prefix")
    validate_vma_api_key_prefix(token)


def hash_vma_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_vma_api_key_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    if isinstance(scopes, (str, bytes)):
        raise ValueError("scopes must be a collection of scope names")
    normalized = tuple(
        sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
    )
    unknown = set(normalized) - KNOWN_VMA_API_KEY_SCOPES
    if unknown:
        raise ValueError(f"Unknown VMA API key scopes: {', '.join(sorted(unknown))}")
    if not normalized:
        raise ValueError("At least one VMA API key scope is required")
    return normalized


def vma_api_key_is_expired(
    api_key: VmaApiKey,
    *,
    now: datetime | None = None,
) -> bool:
    if api_key.expires_at is None:
        return False
    expires_at = _as_utc(api_key.expires_at)
    return expires_at <= _as_utc(now or datetime.now(timezone.utc))


async def create_vma_api_key(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    token: str | None = None,
    allow_legacy_token: bool = False,
    scopes: Iterable[str] = DEFAULT_VMA_API_KEY_SCOPES,
    expires_at: datetime | None = None,
    created_by: str | None = None,
    replaces_key_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[VmaApiKey, str]:
    normalized_name = str(name).strip()
    if not normalized_name:
        raise ValueError("name must not be blank")
    if len(normalized_name) > 255:
        raise ValueError("name must be at most 255 characters")

    plaintext = token or generate_vma_api_key()
    if token is not None:
        if allow_legacy_token and is_legacy_vma_api_key(plaintext):
            validate_legacy_vma_api_key(plaintext)
        else:
            validate_vma_api_key_prefix(plaintext)

    if replaces_key_id is not None:
        replaced = await get_vma_api_key(
            db,
            organization_id=organization_id,
            key_id=replaces_key_id,
        )
        if replaced is None:
            raise ValueError(
                "replaces_key_id must identify a VMA API key in the same Organization"
            )

    normalized_expiry = _future_expiry(expires_at)
    api_key = VmaApiKey(
        id=new_id("key"),
        organization_id=organization_id,
        name=normalized_name,
        key_hash=hash_vma_api_key(plaintext),
        prefix=plaintext[:DISPLAYED_VMA_API_KEY_PREFIX_LENGTH],
        scopes=list(normalize_vma_api_key_scopes(scopes)),
        expires_at=normalized_expiry,
        created_by=created_by,
        replaces_key_id=replaces_key_id,
        metadata_=dict(metadata or {}),
    )
    db.add(api_key)
    await db.flush()
    return api_key, plaintext


async def get_vma_api_key_by_token(
    db: AsyncSession,
    token: str,
    *,
    include_inactive: bool = False,
) -> VmaApiKey | None:
    stmt = select(VmaApiKey).where(VmaApiKey.key_hash == hash_vma_api_key(token))
    if not include_inactive:
        now = datetime.now(timezone.utc)
        stmt = stmt.where(
            VmaApiKey.revoked_at.is_(None),
            or_(VmaApiKey.expires_at.is_(None), VmaApiKey.expires_at > now),
        )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_vma_api_key(
    db: AsyncSession,
    *,
    organization_id: str,
    key_id: str,
    include_revoked: bool = True,
    for_update: bool = False,
) -> VmaApiKey | None:
    stmt = select(VmaApiKey).where(
        VmaApiKey.id == key_id,
        VmaApiKey.organization_id == organization_id,
    )
    if not include_revoked:
        stmt = stmt.where(VmaApiKey.revoked_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_vma_api_keys(
    db: AsyncSession,
    *,
    organization_id: str,
    include_revoked: bool = True,
) -> list[VmaApiKey]:
    stmt = select(VmaApiKey).where(VmaApiKey.organization_id == organization_id)
    if not include_revoked:
        stmt = stmt.where(VmaApiKey.revoked_at.is_(None))
    result = await db.execute(stmt.order_by(VmaApiKey.created_at.desc(), VmaApiKey.id.desc()))
    return list(result.scalars().all())


# How stale `last_used_at` is allowed to get. It answers "is this key still in
# use", which nothing reads in real time and no one asks to the minute — so
# most requests can leave it alone entirely.
LAST_USED_RESOLUTION = timedelta(minutes=1)


def last_used_is_stale(api_key: VmaApiKey, *, now: datetime | None = None) -> bool:
    """Whether this key's `last_used_at` is old enough to be worth rewriting."""
    if api_key.last_used_at is None:
        return True
    moment = now or datetime.now(timezone.utc)
    return moment - api_key.last_used_at >= LAST_USED_RESOLUTION


async def touch_vma_api_key(db: AsyncSession, api_key: VmaApiKey) -> VmaApiKey:
    """Record that this key was just used.

    Do not call this on the request's own session. Every request presenting one
    key writes this one row, and a row written inside a transaction stays
    locked until that transaction commits — so a request that goes on to spend
    eight seconds copying a file into a sandbox makes every other request on
    the same key wait those eight seconds first, at the door, before doing any
    work of its own. Measured: three concurrent attachments waited 0s, 7.2s and
    14.0s here, for a timestamp none of them read.

    `record_vma_api_key_use` is the caller-facing version and owns that rule.
    """
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key


async def record_vma_api_key_use(api_key_id: str) -> None:
    """Stamp `last_used_at` in a transaction of its own, if it is stale.

    Its own session, so the row is locked for one statement rather than for
    however long the request that triggered it turns out to take. Failures are
    swallowed: this is bookkeeping, and a request that did its work should not
    fail because a timestamp did not get written.
    """
    from app.db.engine import session_scope

    try:
        async with session_scope() as db:
            api_key = await db.get(VmaApiKey, api_key_id)
            if api_key is None or not last_used_is_stale(api_key):
                return
            api_key.last_used_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception:
        logger.warning("vma_api_key_touch_failed", api_key_id=api_key_id, exc_info=True)


async def revoke_vma_api_key(
    db: AsyncSession,
    api_key: VmaApiKey,
    *,
    revoked_by: str | None = None,
    reason: str | None = None,
) -> VmaApiKey:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(VmaApiKey)
        .where(
            VmaApiKey.id == api_key.id,
            VmaApiKey.revoked_at.is_(None),
        )
        .values(
            revoked_at=now,
            revoked_by=revoked_by,
            revocation_reason=reason,
            updated_at=now,
        )
    )
    await db.refresh(api_key)
    return api_key


async def rotate_vma_api_key(
    db: AsyncSession,
    *,
    organization_id: str,
    key_id: str,
    token: str | None = None,
    created_by: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[VmaApiKey, str]:
    """Issue a successor while leaving the current key active for cutover.

    The caller revokes the old key explicitly after every consumer has switched
    to the returned plaintext. This is what permits a no-downtime overlap.
    """
    current = await get_vma_api_key(
        db,
        organization_id=organization_id,
        key_id=key_id,
        for_update=True,
    )
    if current is None:
        raise LookupError(f"VMA API key {key_id} does not exist in {organization_id}")
    if current.revoked_at is not None:
        raise ValueError("A revoked VMA API key cannot be rotated")
    if vma_api_key_is_expired(current):
        raise ValueError("An expired VMA API key cannot be rotated")

    replacement, plaintext = await create_vma_api_key(
        db,
        organization_id=organization_id,
        name=current.name,
        token=token,
        scopes=current.scopes,
        expires_at=expires_at,
        created_by=created_by,
        replaces_key_id=current.id,
        metadata=dict(current.metadata_),
    )
    return replacement, plaintext


def _future_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    normalized = _as_utc(expires_at)
    if normalized <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import VMA_API_SCOPE, VmaApiKey
from app.utils.id_generator import new_id


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

LIVE_VMA_API_KEY_PREFIX = "vma_live_"
TEST_VMA_API_KEY_PREFIX = "vma_test_"
KNOWN_VMA_API_KEY_PREFIXES = (
    LIVE_VMA_API_KEY_PREFIX,
    TEST_VMA_API_KEY_PREFIX,
)
DISPLAYED_VMA_API_KEY_PREFIX_LENGTH = 17
MINIMUM_VMA_API_KEY_RANDOM_LENGTH = 32


def vma_api_key_prefix(*, app_env: str | None = None) -> str:
    environment = app_env if app_env is not None else get_settings().app_env
    if str(environment).strip().lower() == "production":
        return LIVE_VMA_API_KEY_PREFIX
    return TEST_VMA_API_KEY_PREFIX


def generate_vma_api_key(*, app_env: str | None = None) -> str:
    return f"{vma_api_key_prefix(app_env=app_env)}{secrets.token_urlsafe(32)}"


def validate_vma_api_key_prefix(token: str, *, app_env: str | None = None) -> None:
    expected_prefix = vma_api_key_prefix(app_env=app_env)
    if not token.startswith(KNOWN_VMA_API_KEY_PREFIXES):
        raise ValueError("api_key must use the vma_live_ or vma_test_ prefix")
    if not token.startswith(expected_prefix):
        raise ValueError(f"api_key must use the {expected_prefix} prefix for this environment")
    if len(token) < len(expected_prefix) + MINIMUM_VMA_API_KEY_RANDOM_LENGTH:
        raise ValueError(
            "api_key must contain at least "
            f"{MINIMUM_VMA_API_KEY_RANDOM_LENGTH} characters after the prefix"
        )


def is_legacy_vma_api_key(token: str) -> bool:
    return token.startswith("vma_") and not token.startswith(KNOWN_VMA_API_KEY_PREFIXES)


def validate_legacy_vma_api_key(token: str) -> None:
    if not is_legacy_vma_api_key(token):
        raise ValueError("legacy api_key must use the vma_ prefix")
    if len(token) < len("vma_") + MINIMUM_VMA_API_KEY_RANDOM_LENGTH:
        raise ValueError(
            "legacy api_key must contain at least "
            f"{MINIMUM_VMA_API_KEY_RANDOM_LENGTH} characters after the prefix"
        )


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


async def touch_vma_api_key(db: AsyncSession, api_key: VmaApiKey) -> VmaApiKey:
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key


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

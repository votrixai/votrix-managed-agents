import hashlib
import secrets
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ApiKey
from app.ids import new_id
from app.organization import resolve_organization_id

API_SCOPE = "api"
API_KEYS_MANAGE_SCOPE = "api_keys:manage"
WORKER_SCOPE = "worker"
KNOWN_API_KEY_SCOPES = frozenset({API_SCOPE, API_KEYS_MANAGE_SCOPE, WORKER_SCOPE})
DEFAULT_API_KEY_SCOPES = (API_SCOPE,)
LIVE_API_KEY_PREFIX = "vma_live_"
TEST_API_KEY_PREFIX = "vma_test_"
KNOWN_API_KEY_PREFIXES = (LIVE_API_KEY_PREFIX, TEST_API_KEY_PREFIX)
DISPLAYED_API_KEY_PREFIX_LENGTH = 17
MINIMUM_API_KEY_RANDOM_LENGTH = 32


def api_key_prefix(*, app_env: str | None = None) -> str:
    """Return the public key prefix for an application environment.

    Only an exact production environment emits live-looking credentials. Every
    other value fails safely to the test prefix.
    """

    environment = app_env if app_env is not None else get_settings().app_env
    if str(environment).strip().lower() == "production":
        return LIVE_API_KEY_PREFIX
    return TEST_API_KEY_PREFIX


def generate_api_key(*, app_env: str | None = None) -> str:
    return f"{api_key_prefix(app_env=app_env)}{secrets.token_urlsafe(32)}"


def validate_api_key_prefix(token: str, *, app_env: str | None = None) -> None:
    """Validate a newly supplied environment-aware operator key."""

    expected_prefix = api_key_prefix(app_env=app_env)
    if token.startswith(KNOWN_API_KEY_PREFIXES):
        if not token.startswith(expected_prefix):
            raise ValueError(
                f"api_key must use the {expected_prefix} prefix for this environment"
            )
        if len(token) < len(expected_prefix) + MINIMUM_API_KEY_RANDOM_LENGTH:
            raise ValueError(
                "api_key must contain at least "
                f"{MINIMUM_API_KEY_RANDOM_LENGTH} characters after the prefix"
            )
        return
    raise ValueError("api_key must use the vma_live_ or vma_test_ prefix")


def is_legacy_api_key(token: str) -> bool:
    return token.startswith("vma_") and not token.startswith(KNOWN_API_KEY_PREFIXES)


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_api_key(
    db: AsyncSession,
    *,
    name: str,
    organization_id: str | None = None,
    token: str | None = None,
    scopes: Iterable[str] = DEFAULT_API_KEY_SCOPES,
    expires_at: datetime | None = None,
    created_by: str | None = None,
    replaces_key_id: str | None = None,
    metadata: dict | None = None,
) -> tuple[ApiKey, str]:
    plaintext = token or generate_api_key()
    normalized_scopes = normalize_api_key_scopes(scopes)
    api_key = ApiKey(
        id=new_id("key"),
        organization_id=resolve_organization_id(organization_id),
        name=name,
        key_hash=hash_api_key(plaintext),
        prefix=plaintext[:DISPLAYED_API_KEY_PREFIX_LENGTH],
        scopes=list(normalized_scopes),
        expires_at=expires_at,
        created_by=created_by,
        replaces_key_id=replaces_key_id,
        metadata_=metadata or {},
    )
    db.add(api_key)
    await db.flush()
    return api_key, plaintext


async def get_api_key_by_token(
    db: AsyncSession,
    token: str,
    *,
    include_archived: bool = False,
) -> ApiKey | None:
    stmt = select(ApiKey).where(ApiKey.key_hash == hash_api_key(token))
    if not include_archived:
        stmt = stmt.where(ApiKey.archived_at.is_(None), ApiKey.revoked_at.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_api_key(
    db: AsyncSession,
    key_id: str,
    *,
    organization_id: str | None = None,
    include_revoked: bool = True,
    for_update: bool = False,
) -> ApiKey | None:
    stmt = select(ApiKey).where(
        ApiKey.id == key_id,
        ApiKey.organization_id == resolve_organization_id(organization_id),
    )
    if not include_revoked:
        stmt = stmt.where(ApiKey.archived_at.is_(None), ApiKey.revoked_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_api_keys(
    db: AsyncSession,
    *,
    organization_id: str | None = None,
    include_revoked: bool = True,
) -> list[ApiKey]:
    stmt = select(ApiKey).where(ApiKey.organization_id == resolve_organization_id(organization_id))
    if not include_revoked:
        stmt = stmt.where(ApiKey.archived_at.is_(None), ApiKey.revoked_at.is_(None))
    result = await db.execute(stmt.order_by(ApiKey.created_at.desc(), ApiKey.id.desc()))
    return list(result.scalars().all())


async def touch_api_key(db: AsyncSession, api_key: ApiKey) -> ApiKey:
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key


async def revoke_api_key(
    db: AsyncSession,
    api_key: ApiKey,
    *,
    revoked_by: str | None = None,
    reason: str | None = None,
    replaced_by_key_id: str | None = None,
) -> ApiKey:
    now = datetime.now(timezone.utc)
    if api_key.revoked_at is None:
        api_key.revoked_at = now
        api_key.archived_at = now
        api_key.revoked_by = revoked_by
        api_key.revocation_reason = reason
        api_key.replaced_by_key_id = replaced_by_key_id
    await db.flush()
    return api_key


async def archive_api_key(db: AsyncSession, api_key: ApiKey) -> ApiKey:
    return await revoke_api_key(db, api_key, reason="archived")


def api_key_is_expired(api_key: ApiKey, *, now: datetime | None = None) -> bool:
    if api_key.expires_at is None:
        return False
    expires_at = api_key.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or datetime.now(timezone.utc))


def normalize_api_key_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(scope).strip() for scope in scopes if str(scope).strip()}))
    unknown = set(normalized) - KNOWN_API_KEY_SCOPES
    if unknown:
        raise ValueError(f"Unknown API key scopes: {', '.join(sorted(unknown))}")
    if not normalized:
        raise ValueError("At least one API key scope is required")
    return normalized

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey
from app.ids import new_id
from app.workspace import workspace_id_or_default


def generate_api_key() -> str:
    return f"vma_{secrets.token_urlsafe(32)}"


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_api_key(
    db: AsyncSession,
    *,
    name: str,
    workspace_id: str | None = None,
    token: str | None = None,
    metadata: dict | None = None,
) -> tuple[ApiKey, str]:
    plaintext = token or generate_api_key()
    api_key = ApiKey(
        id=new_id("key"),
        workspace_id=workspace_id_or_default(workspace_id),
        name=name,
        key_hash=hash_api_key(plaintext),
        prefix=plaintext[:12],
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
        stmt = stmt.where(ApiKey.archived_at.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def touch_api_key(db: AsyncSession, api_key: ApiKey) -> ApiKey:
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key


async def archive_api_key(db: AsyncSession, api_key: ApiKey) -> ApiKey:
    api_key.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key

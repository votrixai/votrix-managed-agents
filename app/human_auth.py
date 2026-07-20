from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException

from app.config import get_settings


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None
    app_metadata: dict[str, Any]
    email_verified: bool = False

    @property
    def is_super_admin(self) -> bool:
        return self.app_metadata.get("super_admin") is True


async def authenticate_user(access_token: str) -> AuthenticatedUser:
    settings = get_settings()
    base_url = settings.vma_supabase_url.rstrip("/")
    api_key = settings.vma_supabase_publishable_key
    if not base_url or not api_key:
        raise HTTPException(status_code=503, detail="Hosted user authentication is not configured")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{base_url}/auth/v1/user",
                headers={"apikey": api_key, "authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Identity provider unavailable") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid user access token")
    payload = response.json()
    user_id = str(payload.get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user access token")
    metadata = payload.get("app_metadata")
    return AuthenticatedUser(
        id=user_id,
        email=payload.get("email"),
        app_metadata=metadata if isinstance(metadata, dict) else {},
        # Supabase `confirmed_at` may represent a confirmed phone number. Only
        # the email-specific timestamp is strong enough for an emailed invite.
        email_verified=bool(payload.get("email_confirmed_at")),
    )


async def require_user(authorization: str | None = Header(None)) -> AuthenticatedUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing user access token")
    return await authenticate_user(authorization.removeprefix("Bearer "))


async def require_super_admin(user: AuthenticatedUser = Depends(require_user)) -> AuthenticatedUser:
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="Super admin required")
    return user

"""Supabase identity verification for the first-party Developer Console."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.config import get_settings


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    app_metadata: dict[str, Any]
    email: str | None = None

    @property
    def is_super_admin(self) -> bool:
        return self.app_metadata.get("super_admin") is True


async def authenticate_user(access_token: str) -> AuthenticatedUser:
    """Resolve a trusted Supabase user from an opaque access token.

    The Console already verifies its cookie-backed session, but VMA repeats the
    verification at its own trust boundary. The token is never decoded or
    trusted from caller-controlled claims locally; Supabase returns the user
    associated with the live token.
    """
    settings = get_settings()
    base_url = settings.vma_supabase_url.strip().rstrip("/")
    publishable_key = settings.vma_supabase_publishable_key.strip()
    if not base_url or not publishable_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Console user authentication is not configured",
        )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{base_url}/auth/v1/user",
                headers={
                    "apikey": publishable_key,
                    "authorization": f"Bearer {access_token}",
                },
            )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity provider unavailable",
        ) from exc

    if response.status_code >= 500:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity provider unavailable",
        )
    if response.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user access token")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity provider returned an invalid response",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity provider returned an invalid response",
        )
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user access token")
    metadata = payload.get("app_metadata")
    email = payload.get("email")
    return AuthenticatedUser(
        id=user_id.strip(),
        app_metadata=metadata if isinstance(metadata, dict) else {},
        email=email.strip().lower()
        if isinstance(email, str) and email.strip()
        else None,
    )

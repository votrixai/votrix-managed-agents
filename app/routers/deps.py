"""Shared FastAPI dependencies for the router layer."""

import asyncio
import hashlib
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Hand out one session per request.

    Closing it discards anything that was never committed, so a failed
    request cannot leave half-written rows behind.
    """
    async with get_session_factory()() as db:
        yield db


async def get_organization_id(
    x_organization_id: Annotated[str, Header()],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """Take the caller's word for which tenant this request belongs to.

    `x-api-key` is accepted but not checked yet. When the API opens up, the
    lookup goes here and no router has to change.
    """
    return x_organization_id


async def get_api_key_id(
    x_api_key: Annotated[str | None, Header()] = None,
) -> str | None:
    """A non-secret attribution handle until API-key rows own the real id.

    The public secret is never stored in Memory Version history. Once request
    authentication resolves a persisted key, this dependency can return that
    row id without changing any Memory route or service signature.
    """
    if not x_api_key:
        return None
    digest = hashlib.sha256(x_api_key.encode()).hexdigest()[:32]
    return f"apikey_{digest}"


async def verify_task_caller(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Let only Cloud Tasks through to the internal endpoints.

    Those endpoints skip the busy check and start work directly, because the
    session was already claimed when the message was accepted. Anything that
    learned the URL could otherwise run turns on any session in any tenant, so
    the caller has to prove it is the queue.

    Under `inline` there is no queue and nothing legitimate calls these, so
    they are simply not there.
    """
    settings = get_settings()
    if settings.turn_dispatch != "cloud":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        # Synchronous, and it fetches Google's signing certificates the first
        # time, so it does not belong on the event loop.
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token,
            google_requests.Request(),
            settings.worker_url.rstrip("/"),
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    # A valid Google token is not enough: any Google account can mint one for
    # this audience. It has to be the service account we told the queue to use.
    if claims.get("email") != settings.tasks_service_account:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not the task service account")


Db = Annotated[AsyncSession, Depends(get_db)]
OrganizationId = Annotated[str, Depends(get_organization_id)]
ApiKeyId = Annotated[str | None, Depends(get_api_key_id)]
TaskCaller = Depends(verify_task_caller)

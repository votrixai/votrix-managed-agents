"""Shared FastAPI dependencies for the router layer."""

import asyncio
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_session_factory
from app.db.models import VmaApiKey
from app.db.queries import vma_api_keys as api_keys_q


async def get_db() -> AsyncIterator[AsyncSession]:
    """Hand out one session per request.

    Closing it discards anything that was never committed, so a failed
    request cannot leave half-written rows behind.
    """
    async with get_session_factory()() as db:
        yield db


Db = Annotated[AsyncSession, Depends(get_db)]

# Strong references to the in-flight `last_used_at` writes. asyncio only holds
# a weak one, so without this the garbage collector is free to cancel a task
# mid-statement.
_touching: set[asyncio.Task[None]] = set()


async def authenticate(
    db: Db,
    x_api_key: Annotated[str | None, Header()] = None,
) -> VmaApiKey:
    """Resolve the key presenting this request, or refuse it.

    Which tenant a request belongs to is read off the key rather than taken
    from a header. A caller that states its own tenant is a caller that can
    state someone else's, and no amount of checking the key afterwards fixes
    that — the two would simply have to be compared, which is the same thing
    as deriving one from the other with an extra way to get it wrong.

    Revoked and expired keys are excluded by the lookup, so both arrive here
    as an unknown key and are refused the same way. Saying which of the three
    it was would tell an unauthenticated caller whether a key ever existed.

    `last_used_at` is stamped beside the request rather than in it. Written on
    this session it would hold a row lock — one row, shared by every request
    presenting this key — until the request committed, which for a file being
    copied into a sandbox is several seconds. Every other request on the key
    would queue behind it at the door. It is also written at most once a
    minute, because it answers "is this key still in use" and nothing reads it
    sooner than that.
    """
    if not x_api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing x-api-key"
        )
    api_key = await api_keys_q.get_vma_api_key_by_token(db, x_api_key)
    if api_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid x-api-key")
    if api_keys_q.last_used_is_stale(api_key):
        task = asyncio.create_task(api_keys_q.record_vma_api_key_use(api_key.id))
        _touching.add(task)
        task.add_done_callback(_touching.discard)
    return api_key


AuthenticatedKey = Annotated[VmaApiKey, Depends(authenticate)]


async def get_organization_id(api_key: AuthenticatedKey) -> str:
    """The tenant this key belongs to, which is the only tenant it can reach."""
    return api_key.organization_id


async def get_api_key_id(api_key: AuthenticatedKey) -> str:
    """The key row's own id, safe to record: it is not the secret."""
    return api_key.id


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


OrganizationId = Annotated[str, Depends(get_organization_id)]
ApiKeyId = Annotated[str, Depends(get_api_key_id)]
TaskCaller = Depends(verify_task_caller)

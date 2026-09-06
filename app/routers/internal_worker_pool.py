"""Authenticated, short Scheduler ticks; no execution in this HTTP request."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from app.config import get_settings
from app.services import worker_pool


async def verify_scaler_caller(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    settings = get_settings()
    if not settings.worker_pool_on_demand or not settings.vma_scaler_audience:
        raise HTTPException(404, "Not found")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token, token, Request(), settings.vma_scaler_audience
        )
    except Exception as exc:
        raise HTTPException(401, "Invalid token") from exc
    if (
        not settings.vma_scaler_service_account
        or claims.get("email") != settings.vma_scaler_service_account
        or claims.get("email_verified") is not True
    ):
        raise HTTPException(403, "Not the scaler service account")


router = APIRouter(include_in_schema=False, dependencies=[Depends(verify_scaler_caller)])


@router.post("/internal/worker-pool/reconcile")
async def reconcile() -> dict[str, str]:
    try:
        return {"state": await worker_pool.reconcile()}
    except Exception as exc:
        worker_pool.logger.exception("worker_pool_reconcile_failed")
        raise HTTPException(503, "Worker pool reconciliation failed") from exc

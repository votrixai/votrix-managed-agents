"""The small control service accepts only its configured Scheduler identity."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.routers import internal_worker_pool as router
from app.scaler import app
from app.services import worker_pool

URL = "https://scaler.example.run.app"
ACCOUNT = "scheduler@test.iam.gserviceaccount.com"


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "TURN_DISPATCH": "pubsub", "PUBSUB_PROJECT": "test", "PUBSUB_TOPIC": "turns",
        "PUBSUB_SUBSCRIPTION": "worker", "VMA_WORKER_POOL_ON_DEMAND": "true",
        "VMA_WORKER_POOL": "projects/test/locations/us-east4/workerPools/worker",
        "VMA_SCALER_AUDIENCE": URL, "VMA_SCALER_SERVICE_ACCOUNT": ACCOUNT,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    verify = Mock(return_value={"email": ACCOUNT, "email_verified": True})
    reconcile = AsyncMock(return_value="idle")
    monkeypatch.setattr(router.id_token, "verify_oauth2_token", verify)
    monkeypatch.setattr(worker_pool, "reconcile", reconcile)
    return verify, reconcile


@pytest.mark.parametrize("case,expected", [
    ("valid", 200), ("missing", 401), ("invalid", 401), ("other_identity", 403),
    ("unverified", 403), ("disabled", 404), ("cloud_failure", 503),
])
async def test_control_auth_and_failure_response(configured, monkeypatch, case, expected):
    verify, reconcile = configured
    if case == "invalid":
        verify.side_effect = ValueError("wrong audience or invalid signature")
    if case == "other_identity":
        verify.return_value["email"] = "someone-else@example.com"
    if case == "unverified":
        verify.return_value["email_verified"] = False
    if case == "disabled":
        monkeypatch.setenv("VMA_WORKER_POOL_ON_DEMAND", "false")
        get_settings.cache_clear()
    if case == "cloud_failure":
        reconcile.side_effect = TimeoutError
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as client:
        response = await client.post("/internal/worker-pool/reconcile", headers=(
            {} if case == "missing" else {"authorization": "Bearer signed-token"}
        ))
    assert response.status_code == expected
    if expected in {200, 503}:
        assert verify.call_args.args[2] == URL
        reconcile.assert_awaited_once()
    else:
        reconcile.assert_not_awaited()


async def test_disabled_wake_never_contacts_cloud(monkeypatch):
    monkeypatch.setenv("TURN_DISPATCH", "inline")
    get_settings.cache_clear()
    request = AsyncMock()
    monkeypatch.setattr(worker_pool, "_request", request)
    await worker_pool.wake()
    request.assert_not_awaited()


def test_on_demand_requires_a_real_pool_resource(configured):
    with pytest.raises(ValidationError, match="full VMA_WORKER_POOL"):
        Settings(vma_worker_pool="worker")

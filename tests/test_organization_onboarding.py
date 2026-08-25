from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import human_auth
from app.control_plane import create_control_plane_app
from app.db.models import ACCOUNT_ACTIVE, MEMBER_ROLE_OWNER
from app.db.queries import accounts as accounts_q
from app.db.queries import organizations as organizations_q
from app.main import app as public_app
from app.models.errors import Conflict
from app.routers.deps import get_db
from app.services import organizations as organizations_service
from tests.conftest import FakeKeys


class FailBeforeMinting(FakeKeys):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def create_key(self, *, name, limit_usd=None, limit_reset="monthly"):
        if not self.failed:
            self.failed = True
            raise RuntimeError("provider unavailable")
        return await super().create_key(
            name=name,
            limit_usd=limit_usd,
            limit_reset=limit_reset,
        )


async def test_initial_onboarding_creates_one_ready_owned_organization(db):
    keys = FakeKeys()

    organization = await organizations_service.provision_initial_organization(
        db,
        requester_user_id="user-new",
        requester_email="Owner@Example.com",
        name="  Acme Labs  ",
        keys=keys,
    )

    account = await accounts_q.get_default_account(
        db,
        organization_id=organization.id,
    )
    member = await organizations_q.get_member(
        db,
        organization_id=organization.id,
        user_id="user-new",
    )
    request = await organizations_q.get_onboarding_request_for_user(
        db,
        requester_user_id="user-new",
    )

    assert organization.name == "Acme Labs"
    assert account is not None
    assert account.status == ACCOUNT_ACTIVE
    assert account.credential is not None
    assert member is not None
    assert member.role == MEMBER_ROLE_OWNER
    assert member.email == "owner@example.com"
    assert request is not None
    assert request.organization_id == organization.id
    assert request.completed_at is not None
    assert len(keys.created) == 1


async def test_replaying_onboarding_returns_the_same_tenant_and_key(db):
    keys = FakeKeys()

    first = await organizations_service.provision_initial_organization(
        db,
        requester_user_id="user-retry",
        requester_email="retry@example.com",
        name="Retry Company",
        keys=keys,
    )
    second = await organizations_service.provision_initial_organization(
        db,
        requester_user_id="user-retry",
        requester_email="retry@example.com",
        name="Retry Company",
        keys=keys,
    )

    assert second.id == first.id
    assert len(keys.created) == 1
    assert len(await organizations_q.list_organizations(db)) == 1


async def test_interrupted_provider_call_resumes_without_a_second_tenant(db):
    keys = FailBeforeMinting()

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await organizations_service.provision_initial_organization(
            db,
            requester_user_id="user-interrupted",
            requester_email=None,
            name="Resumable",
            keys=keys,
        )

    request = await organizations_q.get_onboarding_request_for_user(
        db,
        requester_user_id="user-interrupted",
    )
    assert request is not None
    assert request.organization_id is not None
    assert request.completed_at is None
    assert (
        await organizations_q.get_member(
            db,
            organization_id=request.organization_id,
            user_id="user-interrupted",
        )
        is None
    )

    completed = await organizations_service.provision_initial_organization(
        db,
        requester_user_id="user-interrupted",
        requester_email=None,
        name="Resumable",
        keys=keys,
    )

    assert completed.id == request.organization_id
    assert len(keys.created) == 1
    assert len(await organizations_q.list_organizations(db)) == 1


async def test_live_onboarding_lease_rejects_a_concurrent_provisioner(db):
    request = await organizations_q.create_onboarding_request(
        db,
        requester_user_id="user-concurrent",
        requester_email=None,
        requested_name="One Company",
    )
    await db.commit()
    now = datetime.now(timezone.utc)
    assert await organizations_q.acquire_onboarding_lease(
        db,
        request_id=request.id,
        lease_token="lease_winner",
        now=now,
        expires_at=now + timedelta(minutes=5),
    )
    await db.commit()
    keys = FakeKeys()

    with pytest.raises(Conflict, match="already in progress"):
        await organizations_service.provision_initial_organization(
            db,
            requester_user_id="user-concurrent",
            requester_email=None,
            name="One Company",
            keys=keys,
        )

    assert keys.created == []
    assert await organizations_q.list_organizations(db) == []

    request = await organizations_q.get_onboarding_request_for_user(
        db,
        requester_user_id="user-concurrent",
    )
    request.provisioning_lease_expires_at = now - timedelta(seconds=1)
    await db.commit()

    completed = await organizations_service.provision_initial_organization(
        db,
        requester_user_id="user-concurrent",
        requester_email=None,
        name="One Company",
        keys=keys,
    )

    assert completed.name == "One Company"
    assert len(keys.created) == 1


async def test_onboarding_is_only_for_a_user_without_an_organization(db, org):
    await organizations_q.add_member(
        db,
        organization_id=org,
        user_id="user-member",
        role=MEMBER_ROLE_OWNER,
    )
    await db.commit()
    keys = FakeKeys()

    with pytest.raises(Conflict, match="before joining"):
        await organizations_service.provision_initial_organization(
            db,
            requester_user_id="user-member",
            requester_email=None,
            name="Another tenant",
            keys=keys,
        )

    assert keys.created == []


async def test_replay_cannot_restore_a_removed_owner(db):
    keys = FakeKeys()
    organization = await organizations_service.provision_initial_organization(
        db,
        requester_user_id="user-removed",
        requester_email=None,
        name="Former tenant",
        keys=keys,
    )
    member = await organizations_q.get_member(
        db,
        organization_id=organization.id,
        user_id="user-removed",
    )
    await organizations_q.delete_member(db, member)
    await db.commit()

    with pytest.raises(Conflict, match="already been completed"):
        await organizations_service.provision_initial_organization(
            db,
            requester_user_id="user-removed",
            requester_email=None,
            name="Former tenant",
            keys=keys,
        )


@pytest_asyncio.fixture
async def control_client(db):
    app = create_control_plane_app()
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control.test",
    ) as client:
        yield client


async def test_control_plane_requires_and_forwards_the_supabase_user(
    control_client,
    monkeypatch,
):
    async def authenticated(token: str):
        assert token == "user-token"
        return human_auth.AuthenticatedUser(
            id="user-control",
            app_metadata={},
            email="control@example.com",
        )

    async def provisioned(db, **values):
        assert values == {
            "requester_user_id": "user-control",
            "requester_email": "control@example.com",
            "name": "Control Co",
        }
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            id="org_control",
            name="Control Co",
            created_at=now,
            updated_at=now,
            archived_at=None,
        )

    monkeypatch.setattr(human_auth, "authenticate_user", authenticated)
    monkeypatch.setattr(
        organizations_service,
        "provision_initial_organization",
        provisioned,
    )

    missing = await control_client.post(
        "/internal/organizations",
        json={"name": "Control Co"},
    )
    created = await control_client.post(
        "/internal/organizations",
        headers={"authorization": "Bearer user-token"},
        json={"name": "Control Co"},
    )

    assert missing.status_code == 401
    assert created.status_code == 201
    assert created.json()["id"] == "org_control"
    assert created.headers["cache-control"] == "private, no-store, max-age=0"


async def test_public_api_does_not_mount_organization_creation():
    async with AsyncClient(
        transport=ASGITransport(app=public_app),
        base_url="http://public.test",
    ) as client:
        response = await client.post(
            "/v1/organizations",
            json={"name": "Must stay private"},
        )

    assert response.status_code == 404

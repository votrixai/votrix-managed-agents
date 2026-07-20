from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.db.engine import session_scope
from app.db.models import Organization, OrganizationInvitation
from app.db.queries import organization_invitations as invitations_q
from app.db.queries.organization_owners import is_owner
from app.human_auth import AuthenticatedUser, authenticate_user
from app.invitation_email import InvitationEmailDeliveryError


ORGANIZATION_ID = "org_invitation_test"
SUPER_ADMIN_ID = "user_invitation_superadmin"


async def _user(
    user_id: str = SUPER_ADMIN_ID,
    *,
    email: str = "admin@example.com",
    super_admin: bool = True,
    email_verified: bool = True,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=user_id,
        email=email,
        app_metadata={"super_admin": super_admin},
        email_verified=email_verified,
    )


async def _seed_organization() -> None:
    async with session_scope() as db:
        db.add(
            Organization(
                id=ORGANIZATION_ID,
                slug="invitation-test",
                name="Invitation Test",
                metadata_={},
            )
        )
        await db.commit()


def _headers() -> dict[str, str]:
    return {"authorization": "Bearer header.payload.signature"}


def _token_from_send(send: AsyncMock) -> str:
    invite_url = send.await_args.kwargs["invite_url"]
    token = parse_qs(urlparse(invite_url).fragment)["token"][0]
    assert token
    return token


async def _create_invitation(client, email: str, send: AsyncMock):
    response = await client.post(
        f"/v1/me/organizations/{ORGANIZATION_ID}/invitations",
        headers=_headers(),
        json={"email": email},
    )
    return response, _token_from_send(send) if response.status_code == 201 else None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("confirmation_fields", "expected_verified"),
    [
        ({"email_confirmed_at": "2026-07-20T12:00:00Z"}, True),
        ({"confirmed_at": "2026-07-20T12:00:00Z"}, False),
        ({}, False),
    ],
)
async def test_hosted_auth_reads_verified_email_from_supabase_user_response(
    monkeypatch,
    confirmation_fields,
    expected_verified,
):
    payload = {
        "id": "user_supabase_verified",
        "email": "owner@example.com",
        "app_metadata": {"super_admin": False},
        **confirmation_fields,
    }

    class IdentityClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            assert url == "https://identity.example.test/auth/v1/user"
            assert headers["authorization"] == "Bearer access-token"
            return httpx.Response(
                200,
                json=payload,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(
        "app.human_auth.get_settings",
        lambda: SimpleNamespace(
            vma_supabase_url="https://identity.example.test",
            vma_supabase_publishable_key="publishable-key",
        ),
    )
    monkeypatch.setattr(
        "app.human_auth.httpx.AsyncClient",
        lambda **_kwargs: IdentityClient(),
    )

    user = await authenticate_user("access-token")

    assert user.email == "owner@example.com"
    assert user.email_verified is expected_verified


@pytest.mark.anyio
async def test_superadmin_invites_unregistered_email_with_hashed_token(
    client, monkeypatch
):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(),
    ) as send:
        response, token = await _create_invitation(
            client, "  New.Owner@Example.com  ", send
        )

    assert response.status_code == 201, response.text
    assert response.json()["email"] == "new.owner@example.com"
    assert response.json()["role"] == "owner"
    assert response.json()["status"] == "pending"
    assert token
    send.assert_awaited_once()

    async with session_scope() as db:
        invitation = await db.scalar(
            select(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == ORGANIZATION_ID
            )
        )
        assert invitation is not None
        assert invitation.token_hash == invitations_q.hash_invitation_token(token)
        assert invitation.token_hash != token
        assert invitation.invited_by_user_id == SUPER_ADMIN_ID


@pytest.mark.anyio
async def test_non_superadmin_cannot_manage_invitations(client, monkeypatch):
    await _seed_organization()
    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(super_admin=False),
    )
    response = await client.post(
        f"/v1/me/organizations/{ORGANIZATION_ID}/invitations",
        headers=_headers(),
        json={"email": "owner@example.com"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_invalid_organization_id_returns_not_found(client, monkeypatch):
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    response = await client.get(
        "/v1/me/organizations/not-an-organization/invitations",
        headers=_headers(),
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Organization not found"


@pytest.mark.anyio
async def test_invitation_state_transitions_request_row_locks(monkeypatch):
    db = AsyncMock()
    result = Mock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    await invitations_q.get_organization_invitation(
        db,
        ORGANIZATION_ID,
        "invite_lock_test",
        for_update=True,
    )
    statement = db.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql

    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(invitations_q, "get_organization_invitation", lookup)
    await invitations_q.revoke_invitation(
        db,
        ORGANIZATION_ID,
        "invite_lock_test",
    )
    lookup.assert_awaited_once_with(
        db,
        ORGANIZATION_ID,
        "invite_lock_test",
        for_update=True,
    )

    lookup.reset_mock()
    await invitations_q.prepare_invitation_resend(
        db,
        ORGANIZATION_ID,
        "invite_lock_test",
        token_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(days=14),
    )
    lookup.assert_awaited_once_with(
        db,
        ORGANIZATION_ID,
        "invite_lock_test",
        for_update=True,
    )


@pytest.mark.anyio
async def test_invitation_model_rejects_non_owner_role():
    await _seed_organization()
    async with session_scope() as db:
        db.add(
            OrganizationInvitation(
                id="invite_member_role",
                organization_id=ORGANIZATION_ID,
                email="member@example.com",
                role="member",
                token_hash="b" * 64,
                invited_by_user_id=SUPER_ADMIN_ID,
                expires_at=datetime.now(UTC) + timedelta(days=14),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.anyio
async def test_duplicate_live_invitation_returns_conflict(client, monkeypatch):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(),
    ) as send:
        first, _ = await _create_invitation(client, "owner@example.com", send)
        second = await client.post(
            f"/v1/me/organizations/{ORGANIZATION_ID}/invitations",
            headers=_headers(),
            json={"email": "OWNER@example.com"},
        )
    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.anyio
async def test_email_failure_removes_new_invitation(client, monkeypatch):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(
            side_effect=InvitationEmailDeliveryError("email unavailable")
        ),
    ):
        response = await client.post(
            f"/v1/me/organizations/{ORGANIZATION_ID}/invitations",
            headers=_headers(),
            json={"email": "owner@example.com"},
        )
    assert response.status_code == 503
    async with session_scope() as db:
        assert await db.scalar(select(OrganizationInvitation.id)) is None


@pytest.mark.anyio
async def test_verified_matching_user_accepts_invitation_idempotently(
    client, monkeypatch
):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(),
    ) as send:
        created, token = await _create_invitation(client, "invitee@example.com", send)
    assert created.status_code == 201
    assert token

    invitee_id = "user_invitation_invitee"
    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(
            invitee_id,
            email="invitee@example.com",
            super_admin=False,
        ),
    )
    accepted = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": token},
    )
    repeated = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": token},
    )
    assert accepted.status_code == 200, accepted.text
    assert repeated.status_code == 200, repeated.text
    assert accepted.json()["id"] == ORGANIZATION_ID

    async with session_scope() as db:
        assert await is_owner(db, ORGANIZATION_ID, invitee_id)
        invitation = await db.scalar(select(OrganizationInvitation))
        assert invitation is not None and invitation.accepted_at is not None


@pytest.mark.anyio
async def test_accept_rejects_wrong_or_unverified_email(client, monkeypatch):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(),
    ) as send:
        created, token = await _create_invitation(client, "right@example.com", send)
    assert created.status_code == 201
    assert token

    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(
            "user_wrong",
            email="wrong@example.com",
            super_admin=False,
        ),
    )
    wrong = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": token},
    )
    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(
            "user_unverified",
            email="right@example.com",
            super_admin=False,
            email_verified=False,
        ),
    )
    unverified = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": token},
    )
    assert wrong.status_code == 403
    assert unverified.status_code == 403


@pytest.mark.anyio
async def test_revoke_blocks_acceptance_and_is_listed(client, monkeypatch):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(),
    ) as send:
        created, token = await _create_invitation(client, "owner@example.com", send)
    invitation_id = created.json()["id"]
    revoked = await client.delete(
        f"/v1/me/organizations/{ORGANIZATION_ID}/invitations/{invitation_id}",
        headers=_headers(),
    )
    listed = await client.get(
        f"/v1/me/organizations/{ORGANIZATION_ID}/invitations",
        headers=_headers(),
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert listed.status_code == 200
    assert listed.json()[0]["status"] == "revoked"

    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(
            "user_revoked", email="owner@example.com", super_admin=False
        ),
    )
    accepted = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": token},
    )
    assert accepted.status_code == 404


@pytest.mark.anyio
async def test_resend_rotates_token_and_rolls_back_on_email_failure(
    client, monkeypatch
):
    await _seed_organization()
    monkeypatch.setattr("app.human_auth.authenticate_user", lambda _token: _user())
    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(),
    ) as send:
        created, first_token = await _create_invitation(
            client, "owner@example.com", send
        )
        invitation_id = created.json()["id"]
        send.reset_mock()
        resent = await client.post(
            f"/v1/me/organizations/{ORGANIZATION_ID}/invitations/{invitation_id}/resend",
            headers=_headers(),
        )
        second_token = _token_from_send(send)
    assert resent.status_code == 200
    assert first_token != second_token

    with patch(
        "app.routers.organization_invitations.send_organization_invitation_email",
        new=AsyncMock(
            side_effect=InvitationEmailDeliveryError("email unavailable")
        ),
    ):
        failed = await client.post(
            f"/v1/me/organizations/{ORGANIZATION_ID}/invitations/{invitation_id}/resend",
            headers=_headers(),
        )
    assert failed.status_code == 503

    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(
            "user_resend", email="owner@example.com", super_admin=False
        ),
    )
    old = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": first_token},
    )
    current = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": second_token},
    )
    assert old.status_code == 404
    assert current.status_code == 200


@pytest.mark.anyio
async def test_expired_invitation_cannot_be_accepted(client, monkeypatch):
    await _seed_organization()
    token = invitations_q.generate_invitation_token()
    async with session_scope() as db:
        db.add(
            OrganizationInvitation(
                id="invite_expired",
                organization_id=ORGANIZATION_ID,
                email="expired@example.com",
                role="owner",
                token_hash=invitations_q.hash_invitation_token(token),
                invited_by_user_id=SUPER_ADMIN_ID,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await db.commit()
    monkeypatch.setattr(
        "app.human_auth.authenticate_user",
        lambda _token: _user(
            "user_expired", email="expired@example.com", super_admin=False
        ),
    )
    response = await client.post(
        "/v1/me/invitations/accept",
        headers=_headers(),
        json={"token": token},
    )
    assert response.status_code == 404

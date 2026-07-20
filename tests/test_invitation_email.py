from types import SimpleNamespace

import httpx
import pytest

from app.invitation_email import (
    InvitationEmailDeliveryError,
    send_organization_invitation_email,
)


def _settings(**overrides):
    values = {
        "app_env": "production",
        "vma_resend_api_key": "resend-test-key",
        "vma_email_from": "Votrix <no-reply@mail.votrixai.com>",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.anyio
async def test_invitation_email_uses_resend_and_escapes_html(monkeypatch):
    captured = {}

    class EmailClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return httpx.Response(
                200,
                json={"id": "email_test_123"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.invitation_email.get_settings", _settings)
    monkeypatch.setattr(
        "app.invitation_email.httpx.AsyncClient",
        lambda **_kwargs: EmailClient(),
    )

    message_id = await send_organization_invitation_email(
        to_email="owner@example.com",
        organization_name="Teleport <script>",
        invite_url="https://vma.example.test/invite#token=safe-token",
        invited_by_email="admin@example.com",
    )

    assert message_id == "email_test_123"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer resend-test-key"
    assert captured["payload"]["to"] == ["owner@example.com"]
    assert "Teleport &lt;script&gt;" in captured["payload"]["html"]
    assert "Teleport <script>" not in captured["payload"]["html"]


@pytest.mark.anyio
async def test_invitation_email_rejects_invalid_success_response(monkeypatch):
    class InvalidResponseClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            return httpx.Response(
                200,
                text="not-json",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.invitation_email.get_settings", _settings)
    monkeypatch.setattr(
        "app.invitation_email.httpx.AsyncClient",
        lambda **_kwargs: InvalidResponseClient(),
    )

    with pytest.raises(InvitationEmailDeliveryError, match="invalid response"):
        await send_organization_invitation_email(
            to_email="owner@example.com",
            organization_name="Teleport",
            invite_url="https://vma.example.test/invite#token=safe-token",
            invited_by_email=None,
        )


@pytest.mark.anyio
async def test_hosted_invitation_email_requires_configuration(monkeypatch):
    monkeypatch.setattr(
        "app.invitation_email.get_settings",
        lambda: _settings(vma_resend_api_key="", vma_email_from=""),
    )

    with pytest.raises(InvitationEmailDeliveryError, match="not configured"):
        await send_organization_invitation_email(
            to_email="owner@example.com",
            organization_name="Teleport",
            invite_url="https://vma.example.test/invite#token=safe-token",
            invited_by_email=None,
        )

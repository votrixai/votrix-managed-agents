"""Transactional Organization invitation email delivery."""

from html import escape

import httpx

from app.config import get_settings


RESEND_EMAILS_URL = "https://api.resend.com/emails"


class InvitationEmailDeliveryError(Exception):
    """An invitation could not be delivered."""


def _is_local_environment(app_env: str) -> bool:
    return app_env.lower() in {"", "local", "test", "development"}


async def send_organization_invitation_email(
    *,
    to_email: str,
    organization_name: str,
    invite_url: str,
    invited_by_email: str | None,
) -> str | None:
    settings = get_settings()
    if not settings.vma_resend_api_key or not settings.vma_email_from:
        if _is_local_environment(settings.app_env):
            return None
        raise InvitationEmailDeliveryError(
            "Organization invitation email is not configured"
        )

    escaped_organization = escape(organization_name)
    escaped_url = escape(invite_url, quote=True)
    inviter_line = (
        f"<p>{escape(invited_by_email)} invited you to join this Organization.</p>"
        if invited_by_email
        else ""
    )
    subject = f"You're invited to {organization_name} on Votrix Managed Agents"
    text = (
        f"You've been invited to join {organization_name} on Votrix Managed Agents.\n\n"
        f"Open this link to accept the invitation:\n{invite_url}\n"
    )
    html = (
        f"<p>You've been invited to join <strong>{escaped_organization}</strong> "
        "on Votrix Managed Agents.</p>"
        f"{inviter_line}"
        f'<p><a href="{escaped_url}">Accept invitation</a></p>'
        "<p>If the button does not work, copy and paste this link:</p>"
        f"<p>{escaped_url}</p>"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                RESEND_EMAILS_URL,
                headers={
                    "Authorization": f"Bearer {settings.vma_resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.vma_email_from,
                    "to": [to_email],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
            )
    except httpx.HTTPError as exc:
        raise InvitationEmailDeliveryError(
            "Invitation email provider is unavailable"
        ) from exc
    if response.status_code >= 400:
        raise InvitationEmailDeliveryError(
            f"Invitation email provider returned {response.status_code}"
        )
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise InvitationEmailDeliveryError(
            "Invitation email provider returned an invalid response"
        ) from exc
    message_id = response_payload.get("id") if isinstance(response_payload, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise InvitationEmailDeliveryError(
            "Invitation email provider returned an invalid response"
        )
    return message_id

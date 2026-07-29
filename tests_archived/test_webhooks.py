import base64
import json

import pytest

from app.webhooks import WebhookSignatureError, sign_webhook_payload, unwrap_webhook_event, verify_webhook_signature


def _webhook_key() -> str:
    return "whsec_" + base64.b64encode(b"test-webhook-secret").decode("ascii")


def test_webhook_signature_round_trip_and_unwrap():
    payload = json.dumps(
        {
            "id": "evt_test",
            "type": "event",
            "created_at": "2026-06-20T00:00:00Z",
            "data": {"type": "session.created", "session_id": "sess_test"},
        },
        separators=(",", ":"),
    )
    headers = sign_webhook_payload(payload, key=_webhook_key(), event_id="evt_test", timestamp=1_800_000_000)

    verify_webhook_signature(payload, headers=headers, key=_webhook_key(), now=1_800_000_001)
    assert unwrap_webhook_event(payload, headers=headers, key=_webhook_key(), now=1_800_000_001)["id"] == "evt_test"


def test_webhook_signature_accepts_case_insensitive_headers():
    payload = '{"id":"evt_test","type":"event","created_at":"2026-06-20T00:00:00Z","data":{"type":"vault.deleted"}}'
    headers = sign_webhook_payload(payload, key=_webhook_key(), event_id="evt_test", timestamp=1_800_000_000)
    mixed_case_headers = {name.title(): value for name, value in headers.items()}

    verify_webhook_signature(payload, headers=mixed_case_headers, key=_webhook_key(), now=1_800_000_001)


def test_webhook_signature_rejects_mismatched_payload():
    payload = '{"id":"evt_test","type":"event","created_at":"2026-06-20T00:00:00Z","data":{"type":"session.deleted"}}'
    headers = sign_webhook_payload(payload, key=_webhook_key(), event_id="evt_test", timestamp=1_800_000_000)

    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload.replace("session.deleted", "session.created"),
            headers=headers,
            key=_webhook_key(),
            now=1_800_000_001,
        )


def test_webhook_signature_rejects_stale_timestamp():
    payload = '{"id":"evt_test","type":"event","created_at":"2026-06-20T00:00:00Z","data":{"type":"session.deleted"}}'
    headers = sign_webhook_payload(payload, key=_webhook_key(), event_id="evt_test", timestamp=1_800_000_000)

    with pytest.raises(WebhookSignatureError, match="tolerance"):
        verify_webhook_signature(payload, headers=headers, key=_webhook_key(), now=1_800_000_999)


def test_webhook_unwrap_rejects_non_event_envelope():
    payload = '{"id":"evt_test","type":"not_event"}'
    headers = sign_webhook_payload(payload, key=_webhook_key(), event_id="evt_test", timestamp=1_800_000_000)

    with pytest.raises(WebhookSignatureError, match="event envelope"):
        unwrap_webhook_event(payload, headers=headers, key=_webhook_key(), now=1_800_000_001)

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from app.ids import new_id

WEBHOOK_ID_HEADER = "webhook-id"
WEBHOOK_SIGNATURE_HEADER = "webhook-signature"
WEBHOOK_TIMESTAMP_HEADER = "webhook-timestamp"
WEBHOOK_SIGNATURE_VERSION = "v1"
DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 5 * 60


class WebhookSignatureError(ValueError):
    pass


def sign_webhook_payload(
    payload: str | bytes,
    *,
    key: str | bytes,
    event_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    event_id = event_id or new_id("evt")
    timestamp = int(timestamp or time.time())
    signature = _signature(payload, key=key, event_id=event_id, timestamp=timestamp)
    return {
        WEBHOOK_ID_HEADER: event_id,
        WEBHOOK_TIMESTAMP_HEADER: str(timestamp),
        WEBHOOK_SIGNATURE_HEADER: f"{WEBHOOK_SIGNATURE_VERSION},{signature}",
    }


def verify_webhook_signature(
    payload: str | bytes,
    *,
    headers: Mapping[str, str],
    key: str | bytes,
    tolerance_seconds: int = DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
    now: int | None = None,
) -> None:
    normalized_headers = {name.lower(): value for name, value in headers.items()}
    event_id = normalized_headers.get(WEBHOOK_ID_HEADER)
    timestamp_raw = normalized_headers.get(WEBHOOK_TIMESTAMP_HEADER)
    signature_header = normalized_headers.get(WEBHOOK_SIGNATURE_HEADER)
    if not event_id or not timestamp_raw or not signature_header:
        raise WebhookSignatureError("Missing required webhook signature headers")

    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise WebhookSignatureError("Invalid webhook timestamp") from exc

    current_time = int(now or time.time())
    if tolerance_seconds > 0 and abs(current_time - timestamp) > tolerance_seconds:
        raise WebhookSignatureError("Webhook timestamp is outside the tolerance window")

    expected = _signature(payload, key=key, event_id=event_id, timestamp=timestamp)
    for candidate in _signature_candidates(signature_header):
        if hmac.compare_digest(candidate, expected):
            return
    raise WebhookSignatureError("Webhook signature does not match")


def unwrap_webhook_event(
    payload: str | bytes,
    *,
    headers: Mapping[str, str],
    key: str | bytes,
    tolerance_seconds: int = DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
    now: int | None = None,
) -> dict[str, Any]:
    verify_webhook_signature(
        payload,
        headers=headers,
        key=key,
        tolerance_seconds=tolerance_seconds,
        now=now,
    )
    event = json.loads(_payload_text(payload))
    if not isinstance(event, dict) or event.get("type") != "event":
        raise WebhookSignatureError("Webhook payload is not an event envelope")
    return event


def _signature(payload: str | bytes, *, key: str | bytes, event_id: str, timestamp: int) -> str:
    signed_payload = f"{event_id}.{timestamp}.{_payload_text(payload)}".encode("utf-8")
    digest = hmac.new(_decode_key(key), signed_payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _payload_text(payload: str | bytes) -> str:
    return payload.decode("utf-8") if isinstance(payload, bytes) else payload


def _decode_key(key: str | bytes) -> bytes:
    if isinstance(key, bytes):
        return key
    raw = key[6:] if key.startswith("whsec_") else key
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return key.encode("utf-8")


def _signature_candidates(signature_header: str) -> list[str]:
    candidates: list[str] = []
    for value in signature_header.split():
        if value.startswith(f"{WEBHOOK_SIGNATURE_VERSION},"):
            candidates.append(value.split(",", 1)[1])
    return candidates

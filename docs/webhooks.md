# Webhooks

The current repo does not yet implement webhook endpoint registration or event delivery.

It does include Standard Webhooks-compatible signing helpers in `app.webhooks`, matching the convention used by the Anthropic Python SDK beta webhook unwrap helper:

```text
webhook-id
webhook-timestamp
webhook-signature
```

The signed content is:

```text
{webhook-id}.{webhook-timestamp}.{raw-payload}
```

Use `sign_webhook_payload(...)` for delivery and `verify_webhook_signature(...)` or `unwrap_webhook_event(...)` for receiver-side verification tests.

Production delivery still needs endpoint registration, event routing, retry, idempotency, failure disabling, and secret rotation.

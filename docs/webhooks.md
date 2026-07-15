---
title: Webhooks
description: Current signing helpers and remaining production delivery work.
---

The current repo does not implement webhook endpoint registration or event
delivery, and webhooks are not part of the public-beta GA surface or product
promise. Applications must consume Session SSE or poll durable events.

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

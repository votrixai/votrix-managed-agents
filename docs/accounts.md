---
title: Accounts
description: Separate Agent usage and spending within one Organization.
---

An Account is the usage and spending boundary for Agent work. Every Session
uses exactly one Account, so work assigned to one Account does not appear in
another Account's usage totals.

Use Accounts when customers, teams, products, environments, or workflows need
separate attribution and spending control inside the same Organization.

| What you need | What an Account provides |
| --- | --- |
| Separate attribution | Total, daily, weekly, and monthly USD usage for one Account. |
| Separate limits | An optional USD limit that applies only to that Account. |
| Stable assignment | A Session keeps the Account selected when it was created. |
| Independent lifecycle | Suspending one additional Account does not suspend the others. |
| Retained history | Usage remains readable while an Account is suspended. |

## Default and additional Accounts

Every Organization has one default Account. When Session creation omits
`account_id` or sends it as `null`, VMA selects that Account and returns the
resolved ID as `session.account_id`.

The default Account is suitable when all Agent work belongs to one usage
bucket. Create additional Accounts when work needs to be measured or limited
separately.

<Callout title="Default Account" type="info">

The default Account cannot be suspended because it is the fallback for every
Session created without an explicit `account_id`. Additional Accounts can be
suspended independently.

</Callout>

## Create an Account

Create an additional Account with a name and, optionally, a spending limit:

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/accounts \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "name": "Website Builder",
    "limit_usd": "20.00",
    "idempotency_key": "create-website-builder-account"
  }'
```

- `name` is the label returned in Account responses.
- `limit_usd` is optional. Omit it for no Account-specific limit.
- `idempotency_key` is optional and belongs in the JSON body, not a request
  header. Reuse it when retrying a successful create request to receive the
  original Account instead of creating another one.

A successful create returns `status: "active"` and an `acct_...` ID that can be
used when creating a Session.

See [Create Account](/docs/api/accounts/create_account_v1_accounts_post).

<Callout title="Current Account surface" type="warn">

The current public API does not include Account update or delete operations.
Choose `limit_usd` when creating the Account, and use suspend when the Account
should stop funding work.

</Callout>

## Account status

| Status | Meaning for an API client |
| --- | --- |
| `provisioning` | The Account is not ready and cannot be assigned to a new Session. |
| `active` | The Account can be assigned to Sessions and fund Agent work. |
| `suspended` | Further Agent work is blocked until the Account is resumed. |

`is_default` tells you whether VMA selects the Account when Session creation
omits `account_id`. `limit_usd: null` means the Account is uncapped.

Use [List Accounts](/docs/api/accounts/list_accounts_v1_accounts_get) to return
Accounts oldest first, or
[Retrieve Account](/docs/api/accounts/retrieve_account_v1_accounts__account_id__get)
to read one by ID.

## Assign an Account to a Session

Account selection happens when the Session is created.

Omit `account_id` to use the default Account:

```json
{
  "agent_id": "agent_...",
  "environment_id": "env_..."
}
```

Pass an additional Account when the Session belongs to a separate usage or
spending boundary:

```json
{
  "agent_id": "agent_...",
  "environment_id": "env_...",
  "account_id": "acct_..."
}
```

The resolved Account is pinned for the Session's lifetime. Follow-up messages
and existing Sessions do not silently fall back to another Account.

## Read usage

Read the whole Organization across every Account with:

```text
GET /v1/accounts/usage
```

The response contains the summed lifetime/current-period figures plus an
`accounts` breakdown. It is a live provider snapshot rather than a locally
estimated token cost, so an authorized billing system can consume it without
duplicating VMA's provider-account aggregation rules.

```text
GET /v1/accounts/{account_id}/usage
```

The response is a current snapshot for that Account only:

| Field | Meaning |
| --- | --- |
| `usage_usd` | Cumulative usage for the Account. |
| `usage_daily_usd` | Usage in the current daily window. |
| `usage_weekly_usd` | Usage in the current weekly window. |
| `usage_monthly_usd` | Usage in the current monthly window. |
| `limit_usd` | The Account's limit, or `null` when uncapped. |
| `limit_remaining_usd` | Remaining amount under the limit, or `null` when uncapped. |

Usage remains available for a suspended Account. Treat these values as a
snapshot at request time rather than a receipt for one individual Agent turn.

See [Retrieve Account Usage](/docs/api/accounts/retrieve_account_usage_v1_accounts__account_id__usage_get).

### Read one Session's cumulative usage

```text
GET /v1/sessions/{session_id}/usage
```

This endpoint queries OpenRouter when you call it. VMA does not add up or store
the costs from individual responses. Its `usage_usd` is the Session's cumulative
lifetime spend as of `as_of`, with `snapshot: "cumulative"` and
`source: "openrouter"` making those semantics explicit.

For incremental settlement, keep one last-settled value in your billing system
and debit `max(current_usage_usd - last_settled_usage_usd, 0)`. Daily, weekly,
and monthly windows are not involved, so their boundary resets cannot change a
Session bill.

See [Retrieve Session Usage](/docs/api/sessions/retrieve_session_usage_v1_sessions__session_id__usage_get).

## Suspend and resume

Suspend an additional Account when it should stop funding Agent work:

```text
POST /v1/accounts/{account_id}/suspend
```

Suspension preserves the Account's ID, limit, and usage history. The Account
cannot be selected for a new Session. Existing Sessions stay assigned to it,
but their next Agent work cannot continue until it is resumed. Repeating the
suspend call on an already suspended Account returns it unchanged.

Resume the same Account with:

```text
POST /v1/accounts/{account_id}/resume
```

The Account returns to `active`; existing Sessions can continue without being
recreated. Its ID, limit, and usage history stay the same. Repeating resume on
an already active Account also returns it unchanged.

- [Suspend Account](/docs/api/accounts/suspend_account_v1_accounts__account_id__suspend_post)
- [Resume Account](/docs/api/accounts/resume_account_v1_accounts__account_id__resume_post)

## Account API Reference

| Operation | Purpose |
| --- | --- |
| [Create Account](/docs/api/accounts/create_account_v1_accounts_post) | Create a separate usage and spending boundary. |
| [List Accounts](/docs/api/accounts/list_accounts_v1_accounts_get) | List Accounts oldest first and identify the default. |
| [Retrieve Account](/docs/api/accounts/retrieve_account_v1_accounts__account_id__get) | Read one Account's status and limit. |
| [Retrieve Account Usage](/docs/api/accounts/retrieve_account_usage_v1_accounts__account_id__usage_get) | Read total and current-period USD usage. |
| [Suspend Account](/docs/api/accounts/suspend_account_v1_accounts__account_id__suspend_post) | Stop an additional Account from funding more work. |
| [Resume Account](/docs/api/accounts/resume_account_v1_accounts__account_id__resume_post) | Return a suspended Account to active use. |

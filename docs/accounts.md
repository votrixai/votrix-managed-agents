---
title: Accounts
description: Assign Agent usage, set spending limits, and read current usage.
---

An Account is the usage and spending boundary for Agent work. Assigning
different Sessions to different Accounts lets you track and control their
spend separately.

## Default and additional Accounts

Every Organization has a default Account. When a Session is created without
`account_id`, VMA assigns that default Account.

Create additional Accounts when you want separate usage for a customer, team,
product, or workflow:

```json
{
  "name": "Customer support",
  "limit_usd": 250,
  "idempotency_key": "account-customer-support"
}
```

- `name` is the label shown in Account responses.
- `limit_usd` is an optional spending limit. Omit it for no Account-specific
  limit.
- `idempotency_key` is optional. Reuse the same value when retrying Account
  creation so the retry does not create a second Account.

See [Create Account](/docs/api/accounts/create_account_v1_accounts_post).

## Assign an Account to a Session

Pass `account_id` when creating the Session:

```json
{
  "agent_id": "agent_...",
  "environment_id": "env_...",
  "account_id": "acct_..."
}
```

The Session keeps that Account for its lifetime. A later follow-up message in
the same Session is charged to the same Account.

## Read usage

`GET /v1/accounts/{account_id}/usage` returns:

| Field | Meaning |
| --- | --- |
| `usage_usd` | Total usage for the Account. |
| `usage_daily_usd` | Usage in the current daily window. |
| `usage_weekly_usd` | Usage in the current weekly window. |
| `usage_monthly_usd` | Usage in the current monthly window. |
| `limit_usd` | The Account's limit, or `null` when uncapped. |
| `limit_remaining_usd` | Remaining amount under the limit, or `null` when uncapped. |

See [Retrieve Account Usage](/docs/api/accounts/retrieve_account_usage_v1_accounts__account_id__usage_get).

## Suspend and resume

Suspending an Account prevents it from paying for further Agent work while
keeping its ID, limit, and usage history. Resume it to allow work again.

The default Account cannot be suspended because it is the fallback for
Sessions created without an explicit `account_id`.

- [Suspend Account](/docs/api/accounts/suspend_account_v1_accounts__account_id__suspend_post)
- [Resume Account](/docs/api/accounts/resume_account_v1_accounts__account_id__resume_post)

The complete Accounts reference also includes
[List Accounts](/docs/api/accounts/list_accounts_v1_accounts_get) and
[Retrieve Account](/docs/api/accounts/retrieve_account_v1_accounts__account_id__get).

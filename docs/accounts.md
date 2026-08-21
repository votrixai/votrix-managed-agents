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
| Separate attribution | Normalized token usage from Sessions pinned to one Account. |
| Separate limits | An optional USD limit on a Platform Account. |
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

There are two funding modes:

| Mode | Key source | Model routing | USD usage and limits |
| --- | --- | --- | --- |
| `platform` | VMA creates one isolated OpenRouter key for the Account. | Any model in the VMA catalog through OpenRouter. | Live OpenRouter USD usage and an optional `limit_usd`. |
| `byok` | You supply one or more direct keys: Anthropic, OpenAI, Google, and/or DeepSeek. | Each model uses the matching direct-backend key on this Account. | USD fields are `null`; VMA reports the tokens it observed across all configured backends. |

### Platform Account

Create an additional Platform Account with a name and, optionally, a spending
limit. Omitting `funding` keeps this backward-compatible default:

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

### BYOK Account

Set `funding.type` to `byok` and send one key for each direct backend this
Account should use:

```bash
curl --request POST \
  --url https://api.vma.votrixai.com/v1/accounts \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "name": "Direct Models",
    "idempotency_key": "create-direct-models-account",
    "funding": {
      "type": "byok",
      "credentials": [
        {
          "backend": "anthropic",
          "api_key": "YOUR_ANTHROPIC_API_KEY"
        },
        {
          "backend": "openai",
          "api_key": "YOUR_OPENAI_API_KEY"
        }
      ]
    }
  }'
```

The supported BYOK `backend` values are `anthropic`, `openai`, `google`, and
`deepseek`. Each backend may appear at most once. VMA checks every key with a
read-only endpoint, encrypts it at rest, and returns only the configured
backend names. OAuth is not required for this flow.

OpenRouter is reserved for Platform Accounts. This keeps gateway routing and
VMA-managed key administration out of the direct BYOK path.

Use distinct keys for each BYOK Account. VMA refuses to attach the same
backend key to two Accounts because doing so would merge their activity outside
VMA and break the Account boundary. A BYOK Account cannot set `limit_usd`: VMA
does not own its keys and therefore cannot promise an externally enforced cap.

<Callout title="Exact backend model matching" type="info">

Every model resolves to its catalog provider, then VMA selects that exact
backend key from the same BYOK Account. For example, Claude uses its Anthropic
key and GPT uses its OpenAI key. If the Account has no matching key, Session
creation is rejected before VMA creates the sandbox. VMA never falls back to a
different provider, a Platform key, or another Account.

</Callout>

A successful create returns `status: "active"` and an `acct_...` ID that can be
used when creating a Session.

See [Create Account](/docs/api/accounts/create_account_v1_accounts_post).

### Add or rotate a BYOK key

Use PUT with a direct backend to add its key or replace the key already in that
slot:

```bash
curl --request PUT \
  --url https://api.vma.votrixai.com/v1/accounts/acct_.../credentials/openai \
  --header 'content-type: application/json' \
  --header 'x-api-key: YOUR_API_KEY' \
  --data '{
    "api_key": "YOUR_NEW_OPENAI_API_KEY"
  }'
```

VMA validates the new key before changing stored data. A failed validation
leaves the previous key working. A successful replacement atomically switches
the backend slot, so future model calls use the new key; repeating the same PUT
is idempotent. Updating a suspended Account does not resume it.

Platform Accounts reject this endpoint because VMA administers their
OpenRouter key.

### Remove a BYOK key

Remove one backend with:

```text
DELETE /v1/accounts/{account_id}/credentials/{backend}
```

At least one credential must remain on a BYOK Account. Removing a backend
deletes its stored encrypted key; its historical token usage remains attached
to the Account. Existing Sessions whose selected model needs that backend fail
their next model call until a key is added again. Platform credentials cannot
be removed through this endpoint.

<Callout title="Current Account surface" type="warn">

The public API does not include general Account update or Account delete
operations. Choose `limit_usd` when creating a Platform Account, manage BYOK
keys through the credential endpoints, and use suspend when the Account should
stop funding work.

</Callout>

## Account status

| Status | Meaning for an API client |
| --- | --- |
| `provisioning` | The Account is not ready and cannot be assigned to a new Session. |
| `active` | The Account can be assigned to Sessions and fund Agent work. |
| `suspended` | Further Agent work is blocked until the Account is resumed. |

`is_default` tells you whether VMA selects the Account when Session creation
omits `account_id`. `funding` identifies its mode and configured backend list.
On a Platform Account, `limit_usd: null` means uncapped; it is always `null`
for BYOK.

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

```text
GET /v1/accounts/{account_id}/usage
```

The response is a current snapshot for that Account only:

| Field | Meaning |
| --- | --- |
| `funding` | Funding mode and configured inference backend(s). |
| `usage_usd` | Cumulative OpenRouter usage for a Platform Account; `null` for BYOK. |
| `usage_daily_usd` | Current Platform daily window; `null` for BYOK. |
| `usage_weekly_usd` | Current Platform weekly window; `null` for BYOK. |
| `usage_monthly_usd` | Current Platform monthly window; `null` for BYOK. |
| `limit_usd` | Platform Account limit, or `null` when uncapped or BYOK. |
| `limit_remaining_usd` | Remaining Platform limit, or `null` when uncapped or BYOK. |
| `observed_usage` | Input, output, and total tokens from completed model calls recorded by VMA. |

The token total belongs to the Account because every Session is pinned to its
`account_id`, and every `model.usage` event is written inside that Session.
Calls through Anthropic, OpenAI, Google, and DeepSeek therefore roll up into
one Account total while each event still identifies its actual backend. The
total does not include use of a BYOK key outside VMA. Conversely, VMA
deliberately does not claim a BYOK USD amount: direct backends do not expose
one common, Account-scoped billing contract, and pricing normalized tokens
locally would be an estimate rather than a bill.

Usage remains available for a suspended Account. Treat these values as a
snapshot at request time rather than a receipt for one individual Agent turn.

See [Retrieve Account Usage](/docs/api/accounts/retrieve_account_usage_v1_accounts__account_id__usage_get).

## Suspend and resume

Suspend an additional Account when it should stop funding Agent work:

```text
POST /v1/accounts/{account_id}/suspend
```

Suspension preserves the Account's ID, funding mode, limit, and usage history.
The Account cannot be selected for a new Session. Existing Sessions stay
assigned to it, but their next Agent work cannot continue until it is resumed.
Repeating the suspend call on an already suspended Account returns it unchanged.

Resume the same Account with:

```text
POST /v1/accounts/{account_id}/resume
```

The Account returns to `active`; existing Sessions can continue without being
recreated. Its ID, limit, and usage history stay the same. Repeating resume on
an already active Account also returns it unchanged.

For a Platform Account, suspend and resume also disable and re-enable the
managed OpenRouter key. For BYOK, suspension is local to VMA—the user still
owns the keys and may use them elsewhere—and resume validates every stored key
again before making the Account active.

`read_image` follows the same funding boundary and never falls back to a VMA
key. A Platform Account uses its managed OpenRouter key. A BYOK Account uses
its Google key, even when the Agent itself is running through another configured
backend; without a Google key it receives an unavailable tool result rather
than a hidden cross-Account charge.

- [Suspend Account](/docs/api/accounts/suspend_account_v1_accounts__account_id__suspend_post)
- [Resume Account](/docs/api/accounts/resume_account_v1_accounts__account_id__resume_post)

## Account API Reference

| Operation | Purpose |
| --- | --- |
| [Create Account](/docs/api/accounts/create_account_v1_accounts_post) | Create a separate usage and spending boundary. |
| [List Accounts](/docs/api/accounts/list_accounts_v1_accounts_get) | List Accounts oldest first and identify the default. |
| [Retrieve Account](/docs/api/accounts/retrieve_account_v1_accounts__account_id__get) | Read one Account's status and limit. |
| [Add or replace BYOK key](/docs/api/accounts/set_byok_model_credential_v1_accounts__account_id__credentials__backend__put) | Add or rotate one direct backend key. |
| [Remove BYOK key](/docs/api/accounts/delete_byok_model_credential_v1_accounts__account_id__credentials__backend__delete) | Remove one direct backend key while keeping at least one. |
| [Retrieve Account Usage](/docs/api/accounts/retrieve_account_usage_v1_accounts__account_id__usage_get) | Read Platform USD usage and VMA-observed token usage. |
| [Suspend Account](/docs/api/accounts/suspend_account_v1_accounts__account_id__suspend_post) | Stop an additional Account from funding more work. |
| [Resume Account](/docs/api/accounts/resume_account_v1_accounts__account_id__resume_post) | Return a suspended Account to active use. |

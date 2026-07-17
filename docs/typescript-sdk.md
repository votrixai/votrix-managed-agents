---
title: TypeScript SDK
description: Use the server-side @votrix/sdk client for native Managed Agents resources, Session funding, raw usage, pagination, SSE, and downloads.
---

`@votrix/sdk` is the native, server-side TypeScript client for Votrix Managed
Agents. It requires Node.js 22 or newer and provides ESM and CommonJS exports.

## Install and connect

```bash
cd sdks/typescript && npm ci && npm run build

# Then run this from the consuming Node.js project:
cd /path/to/your-node-project
npm install /absolute/path/to/votrix-managed-agents/sdks/typescript
```

The first npm release is still pending. After publication, install it with
`npm install @votrix/sdk`.

```ts
import Votrix from "@votrix/sdk";

const client = new Votrix({
  apiKey: process.env.VOTRIX_API_KEY,
  baseURL: process.env.VOTRIX_BASE_URL,
});
```

The constructor reads `VOTRIX_API_KEY` and `VOTRIX_BASE_URL` automatically
when they are not passed. The key and URL must belong to the same deployment.
The default authentication scheme is `x-api-key`; bearer authentication is an
explicit constructor option.

This is not a browser SDK. Organization API keys belong only in trusted
backends, and the client refuses browser initialization. Keep distinct,
least-privileged credentials for local, development, staging, and production.
`VOTRIX_API_KEY` is the SDK's only API-key environment variable; service
configuration is separate.

## Resources

| Client property  | Coverage                                                                |
| ---------------- | ----------------------------------------------------------------------- |
| `apiKeys`        | Organization-key create, list, retrieve, rotate, and revoke.            |
| `agents`         | Agent lifecycle and immutable `versions`.                               |
| `environments`   | Environment lifecycle.                                                  |
| `sessions`       | Session lifecycle, `events`, SSE, and attached `resources`.             |
| `files`          | Upload, metadata, list, streamed download, and delete.                  |
| `skills`         | Skill lifecycle, archives, file inputs, and `versions`.                 |
| `vaults`         | Vault lifecycle and nested `modelCredentials`.                          |
| `modelProviders` | Public model-provider discovery.                                        |
| `usage`          | Raw Organization usage with Session, metric, time, and page filters.    |

Resource and method names use camelCase. Request and response fields preserve
the API's snake_case wire names.

```ts
const agent = await client.agents.create({
  name: "support-agent",
  model: { id: "deepseek/deepseek-v4-pro", provider: "openrouter" },
});

const environment = await client.environments.create({
  name: "runtime",
  config: { type: "cloud" },
});

const session = await client.sessions.create({
  agent: agent.id,
  environment_id: environment.id,
  vault_ids: ["vault_end_user", "vault_shared"],
});
```

Session creation and event submission automatically receive an idempotency
key. Pass `idempotencyKey` in request options when the key must remain stable
across a caller-level retry.

## Pagination and response metadata

Every list method returns `PagePromise<T>`. Await it for one page or iterate it
directly for lazy automatic pagination:

```ts
const page = await client.agents.list({ limit: 25 });
if (page.hasNextPage()) await page.getNextPage();

for await (const agent of client.agents.list({ limit: 100 })) {
  console.log(agent.id);
}
```

Normal calls return `APIPromise<T>`. Use `.withResponse()` for the typed value,
Fetch `Response`, and Anthropic-style `request_id`.

## SSE and binary data

Session event streams reconnect with `Last-Event-ID` and suppress replayed IDs:

```ts
const stream = await client.sessions.events.stream(session.id);
try {
  for await (const event of stream) console.log(event.type, event.data);
} finally {
  await stream.close();
}
```

Uploads accept filesystem paths, `Blob`, `ArrayBuffer`, `Buffer`, and typed
array views. Downloads remain streaming by default:

```ts
const file = await client.files.upload({ file: "./report.pdf" });
const download = await client.files.download(file.id);
await download.writeToFile("./downloaded-report.pdf");
```

Use `iterBytes()` for incremental processing or `bytes()`, `arrayBuffer()`,
`read()`, and `text()` to buffer a response. Close an abandoned stream.
The request timeout applies to connection setup and each binary read; SSE
streams instead remain open until completion, failure, abort, or explicit
close.

## Provider BYOK

The provider catalog exposes stable public IDs. Applications submit an end
user's provider key through the typed, write-only Model-Credential API:

```ts
const provider = await client.modelProviders.retrieve("openrouter");
const vault = await client.vaults.create({ display_name: "User credentials" });

const credential = await client.vaults.modelCredentials.create(vault.id, {
  provider: provider.id,
  api_key: endUserProviderKey,
});

await client.vaults.modelCredentials.rotate(vault.id, credential.id, {
  api_key: rotatedEndUserProviderKey,
});
```

Responses never expose the provider key, authentication payload, or internal
secret-slot name. Sessions receive Vault IDs in preference order; Votrix pins
the selected Credential ID and never switches funding sources during the
Session.

## Session funding and usage

Native callers can explicitly select the create-time funding behavior:

```ts
const session = await client.sessions.create({
  agent: agent.id,
  environment_id: environment.id,
  funding: { type: "platform_credits" },
});
```

The valid values are `byok`, `platform_credits`, and
`organization_default`. Omitting the field keeps the CMA-compatible request
shape and uses the Organization default. The immutable public result is typed
at `session.status_details.model_credential_binding` on Session create,
retrieve, and list responses. Secret material and private platform-key
coordinates are never returned.

List append-only raw usage facts when the trusted Organization backend needs
Session attribution or downstream accounting:

```ts
const page = await client.usage.list({
  session_id: session.id,
  metric: "model_tokens",
  limit: 100,
  "occurred_at[gte]": "2026-07-01T00:00:00Z",
});

for (const fact of page.data) {
  console.log(fact.quantity, fact.unit, fact.provider, fact.model);
}
```

Pagination uses the opaque `next_page` value automatically. The usage API
returns provider-reported facts, not prices, balances, or calculated monetary
costs.

## Errors and release

`APIStatusError` exposes `statusCode`, `errorType`, `errorCode`, `requestID`,
headers, and retry metadata. Typed subclasses cover common HTTP statuses.
Connection, timeout, response-validation, and stream failures use distinct
error classes exported by the package.

From `sdks/typescript`, run `npm ci`, `npm run typecheck`, `npm test`,
`npm run build`, `npm run publint`, and `npm run attw`. CI runs the package and
packed-artifact smoke tests on Node.js 22 and 24. A tag matching
`sdk-typescript-v<package-version>` triggers npm Trusted Publishing through
GitHub OIDC; the workflow does not use an npm token. An `@votrix` organization
owner must first publish the new package interactively with 2FA, then attach
the Trusted Publisher. Create a protected `npm` GitHub environment before
enabling release tags. The private source repository means releases omit npm
provenance.

The package README contains the complete examples and release checklist and is
included in every npm tarball.

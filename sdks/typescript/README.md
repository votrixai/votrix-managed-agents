# Votrix Managed Agents (VMA)

`@votrix/managed-agents` is the server-side TypeScript client for the native
Votrix Managed Agents API. It provides typed resources, automatic cursor
pagination, reconnecting server-sent events, streamed downloads, bounded
retries, and typed Session funding and raw-usage helpers.

The package requires Node.js 22 or newer and ships both ESM and CommonJS
entrypoints.

## Installation

The SDK has not been published to npm yet. Build and install it from this
repository during development:

```bash
cd sdks/typescript
npm ci
npm run build
```

Then install that built directory from the consuming Node.js project:

```bash
npm install /absolute/path/to/votrix-managed-agents/sdks/typescript
```

After the first release, the published installation command will be:

```bash
npm install @votrix/managed-agents
```

## Client setup

Pass the Organization API key and the URL of the same Votrix deployment:

```ts
import Votrix from "@votrix/managed-agents";

const client = new Votrix({
  apiKey: process.env.VMA_API_KEY,
  baseURL: process.env.VMA_BASE_URL,
});
```

`Votrix` is also available as a named export. When both values are already in
the process environment, the constructor can read them directly:

```bash
export VMA_API_KEY="vma_live_..."
export VMA_BASE_URL="https://api.vma.votrixai.com"
```

```ts
import { Votrix } from "@votrix/managed-agents";

const client = new Votrix();
```

`VOTRIX_VMA_API_KEY` and `VOTRIX_VMA_BASE_URL` are supported aliases for the
two canonical variables above. If a canonical variable and its alias are both
set, their values must match; otherwise construction fails with a configuration
error that does not reveal either value. Explicit `apiKey` and `baseURL`
options take priority over environment variables.

The default authentication scheme sends `x-api-key`. A deployment that
explicitly accepts bearer authentication can use `authScheme: "bearer"`.
Call `client.close()` during application shutdown; further requests through a
closed client are rejected.

API resource and method names use TypeScript camelCase. Wire properties retain
their API spelling, including fields such as `environment_id`, `vault_ids`,
and `created_at`.

## Resource surface

The client exposes nine top-level resources:

| Resource                | Main operations                                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `client.apiKeys`        | Create, list, retrieve, rotate, and revoke Organization API keys.                                                           |
| `client.agents`         | Create, retrieve, update, list, and archive Agents; list immutable versions through `agents.versions`.                      |
| `client.environments`   | Create, retrieve, update, list, archive, and delete execution Environments.                                                 |
| `client.sessions`       | Manage Sessions, send/list/stream events through `sessions.events`, and manage attached files through `sessions.resources`. |
| `client.files`          | Upload, inspect, list, stream-download, and delete Files.                                                                   |
| `client.skills`         | Create, retrieve, list, and delete Skills; manage archives and versions through `skills.versions`.                          |
| `client.vaults`         | Manage Vaults and provider credentials through `vaults.modelCredentials`.                                                   |
| `client.modelProviders` | Discover and retrieve the public model-provider catalog.                                                                    |
| `client.usage`          | List Organization-scoped, append-only raw usage facts with Session, metric, time, and opaque-page filters.                  |

A minimal Agent-to-Session flow looks like this:

```ts
const agent = await client.agents.create({
  name: "support-agent",
  model: { id: "deepseek/deepseek-v4-pro", provider: "openrouter" },
  system: "Help the end user clearly and concisely.",
});

const environment = await client.environments.create({
  name: "production-runtime",
  config: { type: "cloud" },
});

const session = await client.sessions.create({
  agent: agent.id,
  environment_id: environment.id,
  vault_ids: ["vault_end_user", "vault_organization_shared"],
});
```

Session creation and event submission receive an automatically generated
`Idempotency-Key`. Supply a stable key in request options when its identity
must survive a caller-level retry or process restart:

```ts
await client.sessions.create(
  {
    agent: agent.id,
    environment_id: environment.id,
  },
  { idempotencyKey: organizationOperationID },
);
```

## Promises and response metadata

Normal resource calls return `APIPromise<T>`, which can be awaited like a
native promise. It also exposes the underlying Fetch response and request ID:

```ts
const result = await client.agents.retrieve(agent.id).withResponse();

console.log(result.data.id);
console.log(result.response.status);
console.log(result.request_id);
```

Per-request options include `headers`, `signal`, `timeout`, `maxRetries`,
`retry`, and `idempotencyKey`. The client defaults to two bounded retries when
a request is replay-safe. Reads are replay-safe by method; write retries need
an idempotency key.

## Pagination

List methods return `PagePromise<T>`. Await it to inspect and advance one page:

```ts
const page = await client.agents.list({ limit: 25 });

for (const agent of page.data) console.log(agent.id);

if (page.hasNextPage()) {
  const nextPage = await page.getNextPage();
  console.log(nextPage.data.length);
}
```

Or iterate over the unawaited `PagePromise` to traverse every page lazily:

```ts
for await (const agent of client.agents.list({ limit: 100 })) {
  console.log(agent.id);
}
```

`page.iterPages()` yields whole pages when page boundaries matter.

## Session event streams

Session events use a single-consumer `EventStream`:

```ts
const stream = await client.sessions.events.stream(session.id, {
  max_reconnects: 3,
});

try {
  for await (const event of stream) {
    console.log(event.type, event.seq, event.data);
  }
} finally {
  await stream.close();
}
```

Unexpected disconnects reconnect up to the configured limit. The stream sends
the last received SSE ID in `Last-Event-ID` and suppresses replayed IDs. Pass
`last_event_id` to resume from an ID held by the application. An `EventStream`
can be iterated only once; breaking iteration or calling `close()` releases the
active response.

## Uploads and downloads

File uploads accept a local filesystem path, `Blob`, `ArrayBuffer`, Node
`Buffer`, or another typed-array view. Use an upload descriptor when the name
or media type is not implicit:

```ts
const uploaded = await client.files.upload({
  file: {
    data: Buffer.from("hello from Votrix\n"),
    filename: "hello.txt",
    mime_type: "text/plain",
  },
});

const archivedSkill = await client.skills.create({
  display_title: "Support workflow",
  archive: "./support-skill.zip",
});

const inlineSkill = await client.skills.create({
  display_title: "Inline workflow",
  files: [
    {
      filename: "SKILL.md",
      content: "# Inline workflow\n",
      mime_type: "text/markdown",
    },
  ],
});
```

String upload values are filesystem paths read by the Node.js process, not
literal file contents. A Skill request must provide either one archive or one
homogeneous file list; it cannot mix JSON file objects with multipart uploads.

Downloads return a streaming-first `BinaryResponse`. Choose one consumption
method per response:

```ts
const download = await client.files.download(uploaded.id);

try {
  for await (const chunk of download.iterBytes()) {
    processChunk(chunk);
  }
} finally {
  await download.close();
}
```

For convenience, `bytes()`, `arrayBuffer()`, `text()`, `read()`, and
`writeToFile(path)` are also available. `filename`, `contentType`, `headers`,
and `statusCode` expose response metadata. Skill-version downloads use the
same wrapper:

```ts
const archive = await client.skills.versions.download(2, {
  skill_id: archivedSkill.id,
});
await archive.writeToFile("./support-skill-v2.zip");
```

The request `timeout` bounds both connection setup and each binary read, so a
stalled download is cancelled without imposing a total-size deadline. Set it
to `0` only when the caller supplies its own `AbortSignal`. SSE streams remain
open until they end, fail, are aborted, or are explicitly closed.

## Provider discovery and BYOK

Discover stable provider IDs instead of exposing or accepting Votrix's
internal secret-slot names:

```ts
const providers = await client.modelProviders.list();
const openrouter = providers.data.find(
  (provider) => provider.id === "openrouter",
);
if (!openrouter)
  throw new Error("OpenRouter is not enabled on this deployment");

const vault = await client.vaults.create({
  display_name: "End-user credentials",
});

const credential = await client.vaults.modelCredentials.create(vault.id, {
  provider: openrouter.id,
  api_key: endUserProviderKey,
  display_name: "Personal OpenRouter key",
});

await client.vaults.modelCredentials.rotate(vault.id, credential.id, {
  api_key: rotatedEndUserProviderKey,
});

const safeCredential = await client.vaults.modelCredentials.retrieve(
  credential.id,
  { vault_id: vault.id },
);
```

`api_key` is a write-only provider credential. Model-Credential responses are
sanitized and cannot contain `api_key`, `auth`, or `secret_name`. Archive and
delete purge the stored provider key before changing lifecycle state.

Pass Vault IDs to Session creation in preference order. Votrix resolves a
credential for the Session's model provider and pins that Credential ID. It
does not switch funding sources inside an existing Session.

## Session funding and raw usage

Native callers can require BYOK, require operator-provisioned platform
funding, or use the Organization's default funding policy:

```ts
const session = await client.sessions.create({
  agent: agent.id,
  environment_id: environment.id,
  funding: { type: "platform_credits" },
});
```

Valid funding values are `byok`, `platform_credits`, and
`organization_default`. Omitting `funding` preserves the CMA-compatible wire
shape and behaves as `organization_default`. The selected source is fixed at
Session creation and is available on create, retrieve, and list responses at
`session.status_details.model_credential_binding`. That public binding exposes
only its source, provider, and safe resource IDs; it never includes provider
key material or private platform-key coordinates.

Platform funding means an operator-provisioned provider key. It is not a
prepaid monetary balance. Read the raw provider facts needed for downstream
accounting through `client.usage`:

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

Usage uses opaque `next_page` cursors and also supports `occurred_at[gt]`,
`occurred_at[lt]`, and `occurred_at[lte]`. Entries contain recorded raw
quantities and provider dimensions; the SDK does not invent an end user,
price, or monetary cost.

## API-key safety and server-only use

This SDK is for trusted server processes and refuses to initialize in a
browser. Organization API keys must never be shipped to an end user's runtime.

- Never embed Organization or provider API keys in browser bundles, mobile
  applications, source control, logs, or error messages.
- Keep local, development, staging, and production credentials separate. A
  local client does not inherit a development credential automatically; its
  `VMA_API_KEY`/`VOTRIX_VMA_API_KEY` and
  `VMA_BASE_URL`/`VOTRIX_VMA_BASE_URL` must deliberately target the same
  deployment.
- `VMA_API_KEY` and `VOTRIX_VMA_API_KEY` are the only API-key environment
  variables read by this SDK. Generic Votrix credentials belong to the main
  product and are intentionally ignored. These client credentials do not
  belong in the VMA service's `.env`; service configuration is separate.
- Give each backend the least-privileged Organization key it needs and rotate
  it through `client.apiKeys`.
- Persist the plaintext `secret` from Organization-key create or rotate only
  once. Read, list, and revoke responses expose safe metadata only.

## Errors

HTTP failures use typed subclasses of `APIStatusError`:

```ts
import { APIStatusError, RateLimitError } from "@votrix/managed-agents";

try {
  await client.sessions.retrieve("session_missing");
} catch (error) {
  if (error instanceof RateLimitError) {
    console.error("retry after", error.retryAfter);
  } else if (error instanceof APIStatusError) {
    console.error(error.statusCode, error.errorCode, error.requestID);
  } else {
    throw error;
  }
}
```

Status subclasses cover `400`, `401`, `403`, `404`, `409`, `422`, `429`, and
server errors. Transport failures use `APIConnectionError` and
`APITimeoutError`; invalid responses use `APIResponseValidationError`; SSE
failures use `APIStreamError`. Status errors expose normalized rate-limit
headers. Avoid blindly logging response bodies even when the SDK has redacted
known request secrets.

## Development and release

Work from this package directory:

```bash
cd sdks/typescript
npm ci
npm run format:check
npm run typecheck
npm test
npm run build
npm run publint
npm run attw
npm pack
```

The build emits ESM, CommonJS, and declarations in `dist`.
`.github/workflows/typescript-sdk.yml` runs the complete package gates and
installs the packed artifact for ESM and CommonJS smoke tests on Node.js 22 and 24.

For a release, update the version in `package.json`, `package-lock.json`, and
`src/version.ts`, run the gates above, and push a matching tag such as
`sdk-typescript-v0.1.0`. The publish workflow verifies the tag against the
package version, rebuilds and retests the tarball, then publishes it with npm
Trusted Publishing.

An npm package owner must configure a Trusted Publisher for repository
`votrixai/votrix-managed-agents`, workflow filename
`typescript-sdk-publish.yml`, and GitHub environment `npm`. The workflow
exchanges GitHub OIDC identity directly with npm and must not be given an
`NPM_TOKEN`. Because `@votrix/managed-agents` does not exist on npm yet, an
`@votrix` organization owner must perform the first interactive, 2FA-protected
`npm publish --access public`, then attach the Trusted Publisher; the tag
workflow handles subsequent releases. Before enabling tags, create the `npm`
GitHub environment with required reviewers and release-ref restrictions. This
repository is private, so the workflow intentionally does not request npm
provenance.

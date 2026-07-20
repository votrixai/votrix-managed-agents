# VMA API router

This Cloudflare Worker is the public, no-cache reverse proxy in front of the
Votrix Managed Agents Cloud Run services. It uses one source file with two
isolated Wrangler environments:

| Wrangler environment | Public custom domain | Cloud Run origin |
| --- | --- | --- |
| `staging` | `staging-api.vma.votrixai.com` | `votrix-managed-agents-staging-…run.app` |
| `production` | `api.vma.votrixai.com` | `votrix-managed-agents-…run.app` |

The Worker validates the exact incoming hostname and requires a bare HTTPS
`*.run.app` origin. It streams request and response bodies (including SSE),
disables both browser and Cloudflare caching, preserves API headers, and
rewrites same-origin `Location` redirects back to the public hostname.
It only forwards `/`, `/openapi.json`, `/health`, `/health/*`, `/v1`, and
`/v1/*`; every other path returns `404` at the edge before origin access.

## Prerequisites

- Node.js 22 or newer
- Wrangler authenticated to Cloudflare account
  `258bb648bcbf054bf3c927d9fe382c7a`
- The target Cloud Run service is publicly invokable. Application API-key
  authentication remains enforced by VMA.

## Validate locally

```bash
npm install
npm run types
npm run check
npm run dry-run:staging
npm run dry-run:production
```

Dry runs only bundle and validate the Worker. They do not mutate the
Cloudflare account.

## Deploy staging

```bash
npm run deploy:staging
```

The `staging` environment binds the Custom Domain
`staging-api.vma.votrixai.com`. Cloudflare provisions the DNS record and an
Advanced Certificate for that exact Custom Domain; do not create a competing
CNAME first.

## Deploy production

The checked-in `env.production.vars.ORIGIN_URL` is the canonical Cloud Run
status URL for `votrix-managed-agents`. After any service replacement that
changes it, update that value and run `npm run types` before deployment.

Re-run `npm run check` and `npm run dry-run:production`, then deploy with the
guarded command:

```bash
npm run deploy:production
```

The guard refuses deployment until the origin is a bare HTTPS `*.run.app`
URL. Do not bypass it with a direct `wrangler deploy` command.

## Verify after deployment

```bash
curl -i https://staging-api.vma.votrixai.com/health
curl -i https://staging-api.vma.votrixai.com/health/db
curl -i https://api.vma.votrixai.com/health
curl -i https://api.vma.votrixai.com/health/db
```

Expected API responses include `Cache-Control: no-store`. Authenticated API
and SSE checks should also be run before considering either environment
ready.

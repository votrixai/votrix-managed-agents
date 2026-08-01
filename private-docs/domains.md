# Domains and Public Entry Points

Internal only. Decided 2026-07-19. This document is the permanent naming and
routing contract for VMA. The target API hostname becomes a compatibility
surface as soon as the first external integration uses it; do not rename it
after that point.

## Live status

The permanent names below became the live production and staging entry points
on 2026-07-19. The API Workers, Cloud Run CORS configuration, Vercel frontends,
Supabase Auth site URLs, and hosted documentation all use this contract. The
execution checklist records the cutover and cleanup evidence.

| Surface | Retired entry point | Permanent live entry point |
|---|---|---|
| Production API | `vma.votrixai.com` (Cloudflare Worker Custom Domain) | `api.vma.votrixai.com` |
| Staging API | `staging-vma.votrixai.com` (Cloudflare Worker Custom Domain) | `staging-api.vma.votrixai.com` |
| Production builder frontend | `vmaapp.votrixai.com` and `vma-developer-app.vercel.app` | `vma.votrixai.com` |
| Staging builder frontend | `staging-vmaapp.votrixai.com` and `vma-developer-app-staging.vercel.app` | `staging.vma.votrixai.com` |
| VMA documentation | `docs.votrixai.com` was configured in metadata and CORS but never deployed | `docs.vma.votrixai.com` |
| Hosted operator API | Production Cloud Run `run.app` URL | Unchanged |

The old custom frontend aliases have been detached from their Vercel
deployments, removed from CORS, and deleted from Cloudflare DNS. They no longer
resolve. The automatically assigned `vercel.app` project domains are provider
identifiers, not supported VMA entry points.

The final Cloudflare certificate inventory was verified on 2026-07-20. Active
Advanced packs cover only `docs.vma.votrixai.com`, `api.vma.votrixai.com`, and
`staging-api.vma.votrixai.com`; the zone's normal Universal SSL pack remains.
No retired Worker Custom Domain certificate pack exists.

## The naming system

`votrixai.com` is the umbrella root. The main product owns the bare first-level
subdomains; each independent product gets a self-contained namespace tree. VMA
does not claim bare `api.votrixai.com` or `docs.votrixai.com`.

```text
votrixai.com
├── app.votrixai.com          main product frontend
├── api.votrixai.com          reserved for the main product API
├── docs.votrixai.com         reserved for umbrella/main-product docs
└── VMA tree
    ├── vma.votrixai.com              builder frontend (Vercel)
    ├── api.vma.votrixai.com          production public API
    ├── docs.vma.votrixai.com         VMA developer docs
    ├── staging.vma.votrixai.com      staging builder frontend
    ├── staging-api.vma.votrixai.com  staging public API
    └── admin.vma.votrixai.com        absent unless all cloaking controls ship
```

Future independent products copy the same shape: `<product>.votrixai.com` for
the human application, `api.<product>.votrixai.com` for the machine API, and
`docs.<product>.votrixai.com` for product-specific developer documentation.
VMA documentation remains separate because VMA and the main product have
different account systems, SDKs, and developer audiences.

## The doors and the paths they admit

One FastAPI codebase implements the routes, but each public hostname is a door
that admits only its intended traffic. A hostname is routing and
defense-in-depth, never the authorization boundary. Organization API keys,
Supabase user or superadmin JWTs, and Cloud Run IAM remain the security
boundaries described in `private-docs/architecture.md`.

| Door | Audience | Paths admitted |
|---|---|---|
| `api.vma.votrixai.com` | SDK integrations and browser calls from the production builder | Exactly `/`, `/openapi.json`, `/health`, `/health/...`, `/v1`, and `/v1/...` |
| `staging-api.vma.votrixai.com` | Staging SDK and builder traffic | The same exact allowlist as production |
| `vma.votrixai.com` | Humans using the production builder | Vercel frontend pages; it is not an API origin |
| `staging.vma.votrixai.com` | Humans using the staging builder | Vercel frontend pages; it is not an API origin |
| `docs.vma.votrixai.com` | Developers reading VMA documentation | Documentation site paths only |
| Production Cloud Run `run.app` URL | Operators | The API app, including `/internal/organizations/...`; superadmin JWT is required by the application |
| Private worker Cloud Run URL | Cloud Tasks and operators performing health checks | `/internal/work/...` through Cloud Run IAM/OIDC, plus service health probes; never an SDK or browser entry point |
| `admin.vma.votrixai.com` | None in the default design | Does not exist unless the complete three-together bundle below ships |

The API Worker must implement the allowlist as segment-aware checks:

```text
pathname == "/"
or pathname == "/openapi.json"
or pathname == "/health" or pathname starts with "/health/"
or pathname == "/v1" or pathname starts with "/v1/"
```

Everything else returns 404 at the edge without an origin fetch. This rejects
`/internal`, `/internal/...`, `/docs`, `/redoc`, `/healthz`, `/v10`, and
arbitrary paths. The application-level GA filter remains narrower than the
edge `/v1/...` door and continues to return 404 for unavailable product routes.
`/v1/me/...` is intentionally admitted by the edge because the builder runs in
the browser and authenticates those requests with a user JWT.

The builder is external traffic: its API URL and requests are visible in each
user's browser. It therefore calls the public API door and never receives the
Cloud Run origin or an origin-only credential.

## Conditional admin host and origin cloaking

`admin.vma.votrixai.com` is not a nicer alias for the operator API. It exists
only as part of origin cloaking: a Cloudflare Worker adds an origin-secret
header and the API rejects direct Cloud Run traffic that lacks it. Cloaking
must ship as one indivisible three-part bundle:

1. The edge injects a rotatable origin-secret header and the API validates it
   before protected traffic reaches a router. Cloud Run health probes retain a
   narrowly defined exemption.
2. `admin.vma.votrixai.com` forwards only `/internal/...` and becomes the
   supported operator entry point.
3. Cloudflare Access protects the admin hostname with company SSO before the
   hostname is exposed.

Never ship only one or two parts. Access without origin rejection is bypassed
through `run.app`; origin rejection without the admin door removes the
operator path; and an admin hostname without Access adds exposure without
providing the intended operator control.

Not implementing cloaking is a legitimate permanent choice, not incomplete
work. The `run.app` hostname is not treated as secret, and a direct request
does not bypass application authentication, Organization isolation, durable
rate limits, quotas, or audit attribution. It bypasses only optional
Cloudflare edge controls such as WAF policy. Cloud Run `maxScale` and the
application's limits bound resource exposure, while avoiding cloaking removes
an origin-secret rotation obligation and a coupled Worker/API failure mode.
Keep the direct operator door permanently if those properties are sufficient.

Adopt the three-part bundle only when at least one concrete requirement
justifies the added coupling: sustained abuse that must be forced through the
edge WAF, a larger operator group, or a compliance rule requiring centralized
Access enforcement.

## Cloudflare Custom Domain certificate behavior

Workers Custom Domains create the DNS record and generate an Advanced
Certificate for the exact target hostname. This matters for the VMA tree:
the normal `*.votrixai.com` Universal SSL wildcard does not cover a deeper name
such as `api.vma.votrixai.com`, but the exact certificate created with that
Worker Custom Domain does. Do not detach the old domain while issuance for the
new domain is pending. Confirm the certificate is Active and covers the exact
hostname, then perform an HTTPS request to the new door.

Deleting a Worker Custom Domain does not delete the Advanced Certificate it
created. After an old Custom Domain is detached and no route needs that
hostname, remove the unused certificate explicitly under Cloudflare
**SSL/TLS > Edge Certificates** or through the certificate-pack API. Leaving
it in place does not route traffic, but creates misleading certificate
inventory. If the generated certificate's defaults ever need customization,
replace it deliberately; do not describe Advanced Certificate Manager as a
fallback for normal Worker Custom Domain issuance.

Platform references:

- [Workers Custom Domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
- [Advanced certificates](https://developers.cloudflare.com/ssl/edge-certificates/advanced-certificate-manager/)

## Coordinated cutover checklist

Run staging first. Production follows only after the complete staging path,
certificate, CORS, browser, SDK, and negative-path checks pass.

### 1. Preflight and add the replacement API doors

- [x] Record current Cloudflare Worker domains, DNS records, certificate-pack
      IDs, Vercel aliases, API origin URLs, and CORS values for rollback.
- [x] Confirm the target API hostnames have no conflicting CNAME records;
      Workers Custom Domains cannot be created on a hostname with an existing
      CNAME.
- [x] Add `staging-api.vma.votrixai.com` to the staging Worker while retaining
      `staging-vma.votrixai.com`. Wait for the exact-host certificate to become
      Active and verify HTTPS before detaching anything.
- [x] Repeat for `api.vma.votrixai.com` and retain `vma.votrixai.com` until the
      replacement production API is verified. If Wrangler is the route source
      of truth, use a temporary dual-domain deployment rather than replacing
      the old route in the same unverified step.

### 2. Update and deploy the API Worker

- [x] Change the canonical hostnames and temporary transition routes in:
      `infra/cloudflare/vma-api-router/src/router.ts`,
      `infra/cloudflare/vma-api-router/wrangler.jsonc`, and
      `infra/cloudflare/vma-api-router/scripts/assert-deployable-origin.mjs`.
- [x] Implement the exact edge path allowlist above. Add negative tests proving
      `/internal`, `/internal/organizations`, `/docs`, `/healthz`, `/v10`, and
      an arbitrary path return 404 without calling the origin.
- [x] Update `infra/cloudflare/vma-api-router/test/router.test.ts` and
      `infra/cloudflare/vma-api-router/README.md`; regenerate
      `infra/cloudflare/vma-api-router/src/worker-configuration.d.ts` from the
      Wrangler configuration with `npm run types` from the router directory
      instead of editing generated types by hand.
- [x] Run the Worker test and deployability checks, deploy staging, and then
      prove every allowed path class reaches the correct staging origin and
      every denied path is stopped at the edge.

### 3. Update this repository's canonical strings

- [x] Change the production API examples and defaults in `README.md`,
      `docs/api/index.mdx`, `scripts/export_openapi.py`,
      `tests/test_documentation_surface.py`, `sdks/typescript/README.md`, and
      the production cases in `sdks/typescript/tests/client.test.ts` to
      `https://api.vma.votrixai.com`.
- [x] Change the staging smoke/performance targets in the active deployment
      configuration and the staging cases in
      `sdks/typescript/tests/client.test.ts` to
      `https://staging-api.vma.votrixai.com`. Update the non-canonical override
      example in `sdks/python/tests/test_client.py` so it does not claim the
      reserved bare `api.votrixai.com` name.
- [x] Regenerate `website/public/openapi/vma.json` with
      `cd website && npm run openapi:sync`; do not hand-edit generated OpenAPI.
      Historical entries in `CHANGELOG.md` may retain hostnames that were true
      for those releases.
- [x] Change documentation metadata in `website/app/layout.tsx` and the hosted
      deployment example in `scripts/gcloud/README.md` to
      `docs.vma.votrixai.com`.
- [x] Replace `VMA_CORS_ORIGINS` in `service.production.yaml`,
      `service.worker.production.yaml`, `service.staging.yaml`, and
      `service.worker.staging.yaml`. The final browser origins are
      `https://vma.votrixai.com`, `https://staging.vma.votrixai.com`, and
      `https://docs.vma.votrixai.com` as appropriate; remove stale
      `votrix.ai`, old `vmaapp`, and Vercel deployment origins only after the
      replacement domains are live.
- [x] Update the exact CORS assertions in `tests/test_cloud_run_config.py` and
      run the documentation, Worker, Cloud Run configuration, SDK, and OpenAPI
      consistency tests.

### 4. Update the frontend repository and Vercel

The frontend repository is `../vma-developer-app`.

- [x] Set the production Vercel value of `NEXT_PUBLIC_VMA_URL` to
      `https://api.vma.votrixai.com` and the staging value to
      `https://staging-api.vma.votrixai.com`. Deploy and smoke-test those builds
      through the existing frontend aliases before moving the canonical
      frontend domains.
- [x] After the new API door is healthy and consumers no longer depend on the
      old API hostname, detach `vma.votrixai.com` and
      `staging-vma.votrixai.com` from the API Workers.
- [x] Inspect the final certificate-pack inventory after detaching the old
      Workers Custom Domains. No retired pack remained to delete; active
      Advanced packs cover only the documentation and two canonical API hosts.
- [x] Attach `vma.votrixai.com` and `staging.vma.votrixai.com` to the correct
      Vercel projects, create the DNS records Vercel specifies without the
      Cloudflare proxy, wait for Vercel TLS issuance, and verify both browser
      applications call the new API domains successfully.
- [x] Remove `vmaapp.votrixai.com`, `staging-vmaapp.votrixai.com`, and Vercel
      deployment domains from CORS after the new frontend domains pass
      preflight checks. Detach both old custom frontend aliases from Vercel.
- [x] Delete the two dangling Cloudflare CNAME records for
      `vmaapp.votrixai.com` and `staging-vmaapp.votrixai.com`. Verify both are
      absent from the Cloudflare API, both authoritative nameservers, and the
      `1.1.1.1` resolver after their 60-second TTL expires.

### 5. Move the documentation hostname

- [x] Deploy Fumadocs through the checked-in Cloudflare Workers Static Assets
      configuration, attach `docs.vma.votrixai.com` as its Custom Domain, and
      verify TLS, canonical metadata, generated Markdown, OpenAPI, and headers.
- [x] If `docs.votrixai.com` is live at cutover time, redirect it only after
      the replacement site is healthy. If it is not live, do not create it
      merely for this migration; keep it reserved for the umbrella product.

### 6. Final verification and cleanup

- [x] From outside the trusted network, test `/`, `/openapi.json`, `/health`,
      `/health/db`, and representative `/v1/...` calls through both API doors.
- [x] Prove `/internal/...` and every non-allowlisted path return an edge 404
      while the direct production `run.app` operator route still applies
      superadmin authentication correctly.
- [ ] Run an authenticated browser flow from each frontend, an SDK smoke test,
      an SSE typewriter-stream test, and a real worker turn in staging and
      production.
- [x] Verify CORS permits only the intended browser/documentation origins and
      that neither Vercel nor Cloudflare has an unexpected redirect loop.
- [x] Confirm old Worker Custom Domains, stale CORS origins, and temporary
      dual-domain Worker logic are removed. Record the final domain inventory.
- [x] Confirm no retired Advanced Certificate pack exists, delete both stale
      frontend CNAME records, and verify every canonical API, frontend, and
      documentation endpoint remains healthy.

## What does not change

The cutover does not rename backend routes or identifiers, change route
authentication, migrate database data, change the API/worker split, or change
the explicit `base_url`/`baseURL` override capability in either SDK.

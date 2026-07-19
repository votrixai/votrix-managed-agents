# Domains and Public Entry Points

Internal only. Decided 2026-07-19. This is the permanent naming plan — the API
hostname is frozen the day the first external client integrates; do not
revisit it after launch.

## The naming system

`votrixai.com` is the umbrella root. The **main product owns the bare
first-level subdomains**; every sub-product gets a **self-contained namespace
tree**. VMA never claims a bare subdomain.

```text
votrixai.com
├── app.votrixai.com          main product frontend (exists)
├── api.votrixai.com          RESERVED for the main product's future API
├── docs.votrixai.com         RESERVED for the umbrella/main product
│                             (freed once VMA docs migrate off it)
└── VMA tree:
    ├── vma.votrixai.com              builder frontend (Vercel)
    ├── api.vma.votrixai.com          public API — the only hostname SDK
    │                                 users ever see
    ├── docs.vma.votrixai.com         VMA developer docs (fumadocs site)
    ├── staging.vma.votrixai.com      staging frontend
    ├── staging-api.vma.votrixai.com  staging API
    └── admin.vma.votrixai.com        DOES NOT EXIST — conditional, see below
```

Future sub-products copy the pattern: `<p>.votrixai.com` for humans,
`api.<p>.votrixai.com` for machines, `docs.<p>.votrixai.com` for their docs.

Docs are per-product (not one shared portal) because VMA and the main product
are **separate developer ecosystems** — different audiences, account systems,
and SDKs. A unified docs portal only wins when products share one ecosystem
(the Stripe/AWS situation); that is not ours.

## The doors: which hostname forwards which paths

There is ONE backend app implementing every path. Hostnames are doors in
front of it; each door forwards only its slice. Hostnames are routing and
defense-in-depth — **the security boundary is always the auth tier**
(API key / user JWT / superadmin JWT / IAM; see `architecture.md`).

| Hostname | Audience | Forwards |
|---|---|---|
| `api.vma.votrixai.com` | SDK clients + builder frontend's API calls | `/v1/*` and `/health*`. The GA middleware keeps undocumented `/v1` paths 404; `/v1/me/*` rides here for the frontend (user JWT). **The Worker returns 404 for `/internal/*` — those paths never pass this door.** |
| `vma.votrixai.com` | Humans (browser) | Builder frontend pages. Not an API host. |
| `docs.vma.votrixai.com` | Humans | VMA developer documentation. |
| Cloud Run `run.app` URL | **Operators only** | Everything, including `/internal/organizations/*`. Superadmin JWT is the boundary — this is the *official* operator entry, not a temporary hack, and it relies on auth, never on the URL being secret. |
| `admin.vma.votrixai.com` | — | Does not exist. Conditional item, below. |

The builder frontend is **external traffic** (it runs in customers' browsers;
its API base URL is visible in the JS bundle), so it goes through the public
edge like SDK traffic — it merely uses a different credential type.

## Conditional: admin host ⟷ origin cloaking (three-together rule)

`admin.vma.votrixai.com` exists for exactly one reason: **origin cloaking**
(the Worker injects a secret header; the app rejects direct `run.app` traffic
without it). The moment cloaking ships, `run.app` stops working for operators
too — so these three ship on the same day, never separately:

1. Origin-secret header in the Worker + rejection middleware in the app
   (health probes exempt).
2. `admin.vma.votrixai.com` forwarding ONLY `/internal/*`.
3. Cloudflare Access (company SSO) in front of the admin host.

Triggers (any one): real abuse traffic that must be forced through the edge
WAF; more than two or three operators; a compliance requirement. Until then,
none of the three exist. Not doing cloaking is a legitimate permanent choice:
app auth is fail-closed and rate limits are DB-backed, so bypassing Cloudflare
costs an attacker nothing security-wise — it only bypasses the optional edge
shield, bounded by API `maxScale`.

## Execution checklist (one coordinated switch — `vma.votrixai.com` changes role from API to frontend)

Cloudflare / DNS:
- [ ] Move the Worker custom domains: `vma.votrixai.com` →
      `api.vma.votrixai.com`; `staging-vma.votrixai.com` →
      `staging-api.vma.votrixai.com`. Verify certificate issuance for the
      multi-level names on staging FIRST (Workers Custom Domains normally
      auto-issue; fallback is Advanced Certificate Manager).
- [ ] Point `vma.votrixai.com` / `staging.vma.votrixai.com` at Vercel
      (DNS-only records so Vercel issues the certificates).
- [ ] Point `docs.vma.votrixai.com` at the docs site; keep
      `docs.votrixai.com` redirecting until content is migrated, then release
      it to the umbrella.

Worker (`infra/cloudflare/vma-api-router/`):
- [ ] Hostname constants in `src/router.ts`, `wrangler.jsonc`,
      `scripts/assert-deployable-origin.mjs`, `test/router.test.ts`, README.
- [ ] New rule: path starts with `/internal/` → 404 before forwarding.

This repo:
- [ ] SDK examples in `README.md` (two occurrences), `docs/api/index.mdx`,
      `docs/deployment-platforms.md` → `https://api.vma.votrixai.com`.
- [ ] `VMA_CORS_ORIGINS` in both service manifests: add
      `https://vma.votrixai.com` (+ staging), update the docs origin, remove
      stale `votrix.ai` / `vercel.app` entries once the frontend domains are
      live; update `tests/test_cloud_run_config.py` pins.

Frontend repo (`vma-builder-app`):
- [ ] API base URL → `https://api.vma.votrixai.com`; attach
      `vma.votrixai.com` / `staging.vma.votrixai.com` on Vercel.

## What does not change

Backend routes and auth (all `/v1` and `/internal` paths stay exactly as
implemented), the SDKs (no hardcoded domain — `base_url` is explicit), the
database, and the deploy pipeline.

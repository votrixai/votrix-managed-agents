# Votrix Managed Agents (VMA)

This directory contains the Fumadocs application for Votrix Managed Agents.
Narrative content remains in the repository-level `docs/` directory so product
changes and documentation can ship in the same pull request. API pages are
generated at build time from `public/openapi/vma.json`.

## Local development

Use Node.js 22 or newer:

```bash
cd website
npm install
npm run dev
```

Open `http://localhost:3000`. Narrative docs live under `/docs`; the interactive
API reference under `/docs/api` is the pre-rewrite snapshot.

## Validation

```bash
cd website
npm run typecheck
npm run lint
npm run build
```

`openapi:sync` has not been migrated to the active `app/` rewrite. It imports
removed pre-rewrite symbols and currently fails. Use the running rewrite's
`/openapi.json` endpoint for its schema; do not publish
`public/openapi/vma.json` as the current API until the exporter is ported.

`npm install` also applies the pinned `patches/fumadocs-openapi+11.2.0.patch`.
It avoids an upstream `structuredClone` failure on complex JSON request forms;
keep the patch version aligned when upgrading `fumadocs-openapi`.

## Deployment

`npm run build` writes a fully static site to `website/out/`. Deploy that
directory to any static host or CDN; a Node.js service is not required.

The production site is deployed as a Cloudflare Worker with static assets at
`https://docs.vma.votrixai.com`. Run `npm run deploy:dry-run` to validate the
artifact and `npm run deploy` to build and publish it. The Worker Custom Domain
owns DNS and exact-host TLS for the documentation hostname.

The build also materializes a Markdown representation for every page at
`/docs/<path>.md` (the docs root is `/docs/index.md`). The older
`/llms.mdx/docs/<path>/content.md` paths remain available for local development
and compatibility. LLM discovery is available at `/llms.txt`,
`/llms-full.txt`, and their `/.well-known/` aliases.

Because a generic static export cannot carry arbitrary response metadata,
configure the deployment CDN to return these headers for `.md`, `llms*.txt`,
`/.well-known/llms*.txt`, and `/openapi/*.json` responses:

```text
X-Content-Type-Options: nosniff
X-Robots-Tag: noindex, nofollow, noarchive, nosnippet
Link: </llms.txt>; rel="llms-txt", </llms-full.txt>; rel="llms-full-txt"
X-Llms-Txt: /llms.txt
```

Serve `.md` and `llms*.txt` as `text/markdown; charset=utf-8`; keep the OpenAPI
document as `application/json`.

The site deliberately uses explicit `.md` URLs instead of HTTP `Accept`
negotiation so it remains portable across static hosts. Verify the production
headers after deployment with `curl -I`.

The historical playground calls the API directly from the browser. The active
rewrite does not currently install CORS middleware, so a cross-origin
playground is part of the pending documentation/API migration.

## Structure

- `app/`: Next.js routes, including the homepage and docs renderer.
- `components/api-page.tsx`: Fumadocs OpenAPI playground renderer.
- `lib/source.ts`: combined Markdown and virtual OpenAPI content source.
- `patches/`: pinned dependency fixes applied after installation.
- `source.config.ts`: Fumadocs MDX collection configuration.
- `public/openapi/vma.json`: pre-rewrite generated OpenAPI snapshot.
- `../docs/`: canonical narrative content and navigation metadata.

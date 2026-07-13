# Votrix documentation site

The Docusaurus application lives in this directory. Documentation content stays
in the repository-level `docs/` directory, so product changes and their
documentation remain in the same pull request.

## Local development

Use Node.js 24 (Docusaurus requires Node.js 20 or newer), then install and start
the site:

```bash
cd website
npm install
npm run start
```

The development server watches both `website/` and `../docs/`.

## Validation

```bash
cd website
npm run typecheck
npm run build
```

The production output is written to `website/build/` and can be served by any
static hosting provider. Update `url` in `docusaurus.config.ts` to match the
canonical documentation domain before publishing.

## Structure

- `docusaurus.config.ts`: site metadata, navigation, and theme configuration.
- `sidebars.ts`: explicit documentation information architecture.
- `src/pages/`: the public landing page.
- `src/css/`: global visual theme.
- `../docs/`: canonical Markdown documentation.

Scalar remains the interactive OpenAPI reference exposed by the running FastAPI
service. Docusaurus owns the guides, concepts, compatibility notes, and
operations documentation.

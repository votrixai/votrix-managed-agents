import { defineConfig, defineDocs } from 'fumadocs-mdx/config';
import { metaSchema, pageSchema } from 'fumadocs-core/source/schema';

export const docs = defineDocs({
  dir: '../docs',
  docs: {
    // Public docs are an explicit contract surface. Internal notes may live in
    // ../docs for the team, but they must never become routes or LLM content.
    files: [
      'index.mdx',
      'quickstart.md',
      'core-concepts.md',
      'accounts.md',
      'agent-versioning.md',
      'memory-stores.md',
      'session-events.md',
      'streaming.md',
      'errors.md',
      'limits.md',
      'api/*.mdx',
    ],
    schema: pageSchema,
    postprocess: {
      includeProcessedMarkdown: true,
    },
  },
  meta: {
    schema: metaSchema,
  },
});

export default defineConfig();

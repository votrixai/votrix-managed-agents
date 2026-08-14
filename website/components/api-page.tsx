'use client';

import { createOpenAPIPage } from 'fumadocs-openapi/ui';
import PlaygroundClient from 'fumadocs-openapi/playground/client';

const betaHeader = 'votrix-managed-agents-2026-04-01';

const streamingPaths = new Set([
  '/v1/sessions/{session_id}/events/stream',
  '/v1/sessions/{session_id}/stream',
  '/v1/sessions/{session_id}/threads/{thread_id}/stream',
]);

function withoutFixedPreviewHeader<T extends { parameters?: unknown[] }>(operation: T): T {
  if (!operation.parameters) return operation;

  return {
    ...operation,
    parameters: operation.parameters.filter((parameter) => {
      if (!parameter || typeof parameter !== 'object') return true;
      const value = parameter as { in?: unknown; name?: unknown };
      return value.in !== 'header' || value.name !== 'votrix-managed-agents-beta';
    }),
  };
}

export const OpenAPIPage = createOpenAPIPage({
  // The public HTTP contract is language-neutral; generated TypeScript types
  // add a language-specific panel without adding a second usable example.
  generateTypeScriptDefinitions: false,
  // Version this prefix whenever persisted Playground defaults become invalid.
  // v2 drops the former server URL cached by browsers.
  storageKeyPrefix: 'votrix-managed-agents-openapi-v2-',
  shikiOptions: {
    themes: {
      light: 'github-light',
      dark: 'vesper',
    },
  },
  schemaUI: {
    // Request components carry reviewed OpenAPI examples. Render them beside
    // the field schema so readers can copy the JSON without first translating
    // a generated TypeScript type or a cURL command.
    showExample: true,
  },
  content: {
    renderAPIExampleLayout({ selector, usageTabs, responseTabs }) {
      return (
        <div className="prose-no-margin">
          <section>
            <p className="mb-2 font-semibold text-fd-card-foreground">Sample request</p>
            {selector}
            {usageTabs}
          </section>
          <section>
            <p className="mb-2 mt-4 font-semibold text-fd-card-foreground">Sample response</p>
            {responseTabs}
          </section>
        </div>
      );
    },
  },
  playground: {
    fetchOptions: {
      requestTimeout: 60,
      onRequestInit(requestInit) {
        const headers = new Headers(requestInit.headers);
        headers.set('votrix-managed-agents-beta', betaHeader);

        return {
          ...requestInit,
          headers,
          credentials: 'omit',
        };
      },
    },
    render({ path, method, operation, pathItem }) {
      if (streamingPaths.has(path)) {
        return (
          <div className="not-prose rounded-xl border bg-fd-card p-4 text-sm text-fd-card-foreground">
            <div className="flex items-center gap-2 font-mono">
              <span className="font-semibold uppercase text-green-600 dark:text-green-400">
                {method}
              </span>
              <span className="overflow-auto text-nowrap text-fd-muted-foreground">{path}</span>
            </div>
            <p className="mt-3 leading-6 text-fd-muted-foreground">
              This endpoint keeps an SSE connection open. Use the cURL or SDK example in the
              request panel to consume the stream; the browser playground intentionally does not
              buffer or send long-lived streaming requests.
            </p>
          </div>
        );
      }

      return (
        <PlaygroundClient
          route={path}
          method={method}
          operation={withoutFixedPreviewHeader(operation)}
          pathItem={pathItem}
          writeOnly
          readOnly={false}
        />
      );
    },
  },
});

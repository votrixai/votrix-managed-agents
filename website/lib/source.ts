import { docs } from 'collections/server';
import { loader } from 'fumadocs-core/source';
import { openapi } from '@/lib/openapi';
import { docsContentRoute, docsRoute } from '@/lib/shared';

export const source = loader(
  {
    docs: docs.toFumadocsSource(),
    openapi: await openapi.staticSource({
      baseDir: 'api/(generated)',
      meta: {
        folderStyle: 'separator',
      },
      groupBy: 'tag',
    }),
  },
  {
    baseUrl: docsRoute,
    plugins: [openapi.loaderPlugin()],
  },
);

export function getPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'content.md'];

  return {
    segments,
    url: `${docsContentRoute}/${segments.join('/')}`,
  };
}

function rewriteRelativeDocLinks(
  markdown: string,
  page: (typeof source)['$inferPage'],
) {
  return markdown.replace(
    /\]\((\.\.?\/[^)\s]+\.md(?:#[^)\s]+)?)\)/g,
    (match, target: string) => {
      const hashIndex = target.indexOf('#');
      const relativePath = hashIndex === -1 ? target : target.slice(0, hashIndex);
      const hash = hashIndex === -1 ? '' : target.slice(hashIndex);
      const segments = page.slugs.slice(0, -1);

      for (const segment of relativePath.split('/')) {
        if (!segment || segment === '.') continue;
        if (segment === '..') {
          segments.pop();
          continue;
        }
        segments.push(segment);
      }

      const filename = segments.at(-1);
      if (!filename?.endsWith('.md')) return match;
      segments[segments.length - 1] = filename.slice(0, -3);
      if (segments.at(-1) === 'index') segments.pop();

      const path = segments.length > 0 ? `${docsRoute}/${segments.join('/')}` : docsRoute;
      return `](${path}${hash})`;
    },
  );
}

export async function getLLMText(page: (typeof source)['$inferPage']) {
  if (page.type === 'openapi') {
    const operation = page.data.getOpenAPIPageProps().operations?.[0];
    const endpoint = operation
      ? `\`${operation.method.toUpperCase()} ${operation.path}\``
      : 'OpenAPI operation';
    const description = page.data.description
      ? `\n\n${page.data.description}`
      : '';

    return [
      `# ${page.data.title} (${page.url})`,
      `${endpoint}${description}`,
      'Full machine-readable schema: [/openapi/vma.json](/openapi/vma.json)',
    ].join('\n\n');
  }

  const processed = rewriteRelativeDocLinks(
    await page.data.getText('processed'),
    page,
  );
  return `# ${page.data.title} (${page.url})\n\n${processed}`;
}

import { docs } from 'collections/server';
import { llms, loader } from 'fumadocs-core/source';
import { openapi } from '@/lib/openapi';
import { renderOpenAPIPageMarkdown } from '@/lib/openapi-markdown';
import {
  appDescription,
  appName,
  docsRoute,
  legacyDocsContentRoute,
} from '@/lib/shared';

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
  return {
    url:
      page.slugs.length === 0
        ? `${docsRoute}/index.md`
        : `${docsRoute}/${page.slugs.join('/')}.md`,
  };
}

export function getLegacyPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'content.md'];

  return {
    segments,
    url: `${legacyDocsContentRoute}/${segments.join('/')}`,
  };
}

/** Keep Copy/Open functional in `next dev`; production builds materialize canonical *.md files. */
export function getServedPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  return process.env.NODE_ENV === 'development'
    ? getLegacyPageMarkdownUrl(page)
    : getPageMarkdownUrl(page);
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
    return renderOpenAPIPageMarkdown({
      title: page.data.title ?? 'API endpoint',
      description: page.data.description,
      pageUrl: page.url,
      props: page.data.getOpenAPIPageProps(),
    });
  }

  const processed = rewriteRelativeDocLinks(
    await page.data.getText('processed'),
    page,
  );
  return `# ${page.data.title} (${page.url})\n\n${processed}`;
}

export function getLLMsIndex() {
  let index = llms(source).index();
  const firstLineEnd = index.indexOf('\n');
  if (firstLineEnd !== -1) {
    index = `${index.slice(0, firstLineEnd)}\n\n> ${appDescription}\n${index.slice(
      firstLineEnd + 1,
    )}`;
  }

  for (const page of source.getPages()) {
    index = index.replaceAll(
      `](${page.url})`,
      `](${getServedPageMarkdownUrl(page).url})`,
    );
  }

  return [
    index.trimEnd(),
    '## OpenAPI specs',
    `- [${appName} OpenAPI schema](/openapi/vma.json): Complete OpenAPI 3.1 schema for the public API.`,
    '## Complete documentation',
    '- [All documentation in one file](/llms-full.txt): Full guides and API reference for LLM context.',
  ].join('\n\n');
}

export async function getLLMsFullText() {
  const pages = await Promise.all(source.getPages().map(getLLMText));
  return [
    `# ${appName} documentation`,
    `> ${appDescription}`,
    `Source index: [/llms.txt](/llms.txt)`,
    ...pages,
  ].join('\n\n');
}

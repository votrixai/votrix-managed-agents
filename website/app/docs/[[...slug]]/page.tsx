import { OpenAPIPage } from '@/components/api-page';
import { getMDXComponents } from '@/components/mdx';
import { PageActions } from '@/components/page-actions';
import { scopeOpenAPIPageProps } from '@/lib/openapi-page';
import { getServedPageMarkdownUrl, source } from '@/lib/source';
import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import { createRelativeLink } from 'fumadocs-ui/mdx';
import {
  DocsBody,
  DocsDescription,
  DocsPage,
  DocsTitle,
} from 'fumadocs-ui/layouts/docs/page';

export default async function Page(props: PageProps<'/docs/[[...slug]]'>) {
  const { slug } = await props.params;
  const page = source.getPage(slug);
  if (!page) notFound();

  const markdownUrl = getServedPageMarkdownUrl(page).url;

  if (page.type === 'openapi') {
    const openAPIPageProps = scopeOpenAPIPageProps(page.data.getOpenAPIPageProps());

    return (
      <DocsPage full className="votrix-api-reference">
        <h1 className="text-[1.75em] font-semibold">{page.data.title}</h1>
        <PageActions markdownUrl={markdownUrl} />
        <DocsBody>
          <OpenAPIPage {...openAPIPageProps} />
        </DocsBody>
      </DocsPage>
    );
  }

  const MDX = page.data.body;
  const isAPIReference = page.url === '/docs/api';

  return (
    <DocsPage
      toc={page.data.toc}
      full={page.data.full}
      className={isAPIReference ? 'votrix-api-reference' : undefined}
    >
      <DocsTitle>{page.data.title}</DocsTitle>
      <DocsDescription className="mb-0">{page.data.description}</DocsDescription>
      <PageActions markdownUrl={markdownUrl} />
      <DocsBody className="docs-prose">
        <MDX
          components={getMDXComponents({
            a: createRelativeLink(source, page),
          })}
        />
      </DocsBody>
    </DocsPage>
  );
}

export function generateStaticParams() {
  return source.generateParams();
}

export async function generateMetadata(
  props: PageProps<'/docs/[[...slug]]'>,
): Promise<Metadata> {
  const { slug } = await props.params;
  const page = source.getPage(slug);
  if (!page) notFound();

  return {
    title: page.data.title,
    description: page.data.description,
    alternates: {
      canonical: page.url,
      types: {
        'text/markdown': getServedPageMarkdownUrl(page).url,
      },
    },
  };
}

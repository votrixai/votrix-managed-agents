import { markdownResponse } from '@/lib/llm-response';
import { getLegacyPageMarkdownUrl, getLLMText, source } from '@/lib/source';
import { notFound } from 'next/navigation';

export const revalidate = false;

export async function GET(
  _request: Request,
  { params }: RouteContext<'/llms.mdx/docs/[[...slug]]'>,
) {
  const { slug } = await params;
  const page = source.getPage(slug?.slice(0, -1));
  if (!page) notFound();

  return markdownResponse(await getLLMText(page), {
    Link: `<${page.url}>; rel="canonical", </llms.txt>; rel="llms-txt", </llms-full.txt>; rel="llms-full-txt"`,
  });
}

export function generateStaticParams() {
  return source.getPages().map((page) => ({
    slug: getLegacyPageMarkdownUrl(page).segments,
  }));
}

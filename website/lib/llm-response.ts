const discoveryLinks = [
  '</llms.txt>; rel="llms-txt"',
  '</llms-full.txt>; rel="llms-full-txt"',
].join(', ');

export const llmResponseHeaders = {
  'Cache-Control': 'public, max-age=0, must-revalidate',
  'Content-Type': 'text/markdown; charset=utf-8',
  Link: discoveryLinks,
  'X-Content-Type-Options': 'nosniff',
  'X-Llms-Txt': '/llms.txt',
  'X-Robots-Tag': 'noindex, nofollow, noarchive, nosnippet',
} as const;

export function markdownResponse(body: BodyInit, headers?: HeadersInit) {
  const responseHeaders = new Headers(llmResponseHeaders);
  if (headers) {
    new Headers(headers).forEach((value, key) => responseHeaders.set(key, value));
  }

  return new Response(body, { headers: responseHeaders });
}

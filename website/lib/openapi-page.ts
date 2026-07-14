import type { OpenAPIPageProps } from 'fumadocs-openapi/ui';

const httpMethods = new Set([
  'delete',
  'get',
  'head',
  'options',
  'patch',
  'post',
  'put',
  'trace',
]);

/**
 * Keep static API pages self-contained without serializing every unrelated path
 * into every page. Components, security schemes, tags, and server metadata stay
 * available so schemas and the request playground work exactly as before.
 */
export function scopeOpenAPIPageProps(props: OpenAPIPageProps): OpenAPIPageProps {
  if (!('payload' in props) || !props.operations?.length) return props;

  const document = props.payload.bundled;
  if (!document.paths) return props;

  const paths: NonNullable<typeof document.paths> = {};
  for (const operation of props.operations) {
    const pathItem = document.paths[operation.path];
    if (!pathItem) continue;

    const method = operation.method.toLowerCase();
    const scopedPathItem = Object.fromEntries(
      Object.entries(pathItem).filter(
        ([key]) => !httpMethods.has(key.toLowerCase()) || key.toLowerCase() === method,
      ),
    );
    paths[operation.path] = scopedPathItem;
  }

  return {
    ...props,
    payload: {
      ...props.payload,
      bundled: {
        ...document,
        paths,
      },
    },
  };
}

import type { OpenAPIPageProps } from 'fumadocs-openapi/ui';

type JsonObject = Record<string, unknown>;

interface OpenAPIMarkdownOptions {
  title: string;
  description?: string;
  pageUrl: string;
  props: OpenAPIPageProps;
}

const openAPISpecUrl = '/openapi/vma.json';

function asObject(value: unknown): JsonObject | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  return value as JsonObject;
}

function asArray(value: unknown): unknown[] | undefined {
  return Array.isArray(value) ? value : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function decodePointerSegment(value: string) {
  return value.replaceAll('~1', '/').replaceAll('~0', '~');
}

function resolveLocalReference(document: JsonObject, value: unknown): JsonObject | undefined {
  const object = asObject(value);
  const ref = asString(object?.$ref);
  if (!object || !ref?.startsWith('#/')) return object;

  let resolved: unknown = document;
  for (const segment of ref.slice(2).split('/').map(decodePointerSegment)) {
    resolved = asObject(resolved)?.[segment];
    if (resolved === undefined) return object;
  }

  const resolvedObject = asObject(resolved);
  return resolvedObject ? { ...resolvedObject, ...object } : object;
}

function referenceName(value: unknown): string | undefined {
  const ref = asString(asObject(value)?.$ref);
  return ref?.startsWith('#/') ? decodePointerSegment(ref.split('/').at(-1) ?? '') : undefined;
}

function escapeTableCell(value: string) {
  return value.replaceAll('|', '\\|').replaceAll('\r\n', '<br>').replaceAll('\n', '<br>');
}

function inlineValue(value: unknown): string {
  if (typeof value === 'string') return `\`${value.replaceAll('`', '\\`')}\``;
  if (value === undefined) return '';

  const serialized = JSON.stringify(value);
  return serialized === undefined ? String(value) : `\`${serialized.replaceAll('`', '\\`')}\``;
}

function schemaType(document: JsonObject, value: unknown, depth = 0): string {
  if (depth > 5) return referenceName(value) ?? 'unknown';

  const schema = resolveLocalReference(document, value);
  if (!schema) return 'unknown';

  const explicitType = schema.type;
  if (Array.isArray(explicitType)) {
    return explicitType.filter((item): item is string => typeof item === 'string').join(' | ');
  }

  if (explicitType === 'array') {
    return `${schemaType(document, schema.items, depth + 1)}[]`;
  }

  if (typeof explicitType === 'string') {
    const format = asString(schema.format);
    return format ? `${explicitType}<${format}>` : explicitType;
  }

  for (const keyword of ['oneOf', 'anyOf', 'allOf'] as const) {
    const members = asArray(schema[keyword]);
    if (!members?.length) continue;

    const separator = keyword === 'allOf' ? ' & ' : ' | ';
    return [...new Set(members.map((member) => schemaType(document, member, depth + 1)))].join(
      separator,
    );
  }

  if (asObject(schema.properties)) return 'object';
  if (asArray(schema.enum)?.length) return 'enum';
  return referenceName(value) ?? 'unknown';
}

function collectReferenceNames(value: unknown, names = new Set<string>(), depth = 0) {
  if (depth > 4) return names;

  const object = asObject(value);
  if (!object) return names;

  const name = referenceName(object);
  if (name) names.add(name);

  for (const keyword of ['oneOf', 'anyOf', 'allOf'] as const) {
    for (const member of asArray(object[keyword]) ?? []) {
      collectReferenceNames(member, names, depth + 1);
    }
  }

  if (object.items) collectReferenceNames(object.items, names, depth + 1);
  return names;
}

function schemaSummary(document: JsonObject, value: unknown): string {
  const names = [...collectReferenceNames(value)];
  const type = schemaType(document, value);
  if (!names.length) return `\`${type}\``;

  const links = names.map(
    (name) => `[\`${name}\`](${openAPISpecUrl}#/components/schemas/${encodeURIComponent(name)})`,
  );
  return `${links.join(', ')} (\`${type}\`)`;
}

function constraintDescription(document: JsonObject, value: unknown) {
  const original = asObject(value) ?? {};
  const schema = resolveLocalReference(document, value) ?? original;
  const parts: string[] = [];
  const description = asString(original.description) ?? asString(schema.description);
  if (description) parts.push(description);

  const enumValues = asArray(original.enum) ?? asArray(schema.enum);
  if (enumValues?.length) parts.push(`Allowed: ${enumValues.map(inlineValue).join(', ')}.`);

  const defaultValue = original.default ?? schema.default;
  if (defaultValue !== undefined) parts.push(`Default: ${inlineValue(defaultValue)}.`);

  const constraints: [string, string][] = [
    ['minimum', 'Minimum'],
    ['maximum', 'Maximum'],
    ['exclusiveMinimum', 'Exclusive minimum'],
    ['exclusiveMaximum', 'Exclusive maximum'],
    ['minLength', 'Minimum length'],
    ['maxLength', 'Maximum length'],
    ['minItems', 'Minimum items'],
    ['maxItems', 'Maximum items'],
    ['pattern', 'Pattern'],
  ];
  for (const [key, label] of constraints) {
    const constraint = original[key] ?? schema[key];
    if (constraint !== undefined) parts.push(`${label}: ${inlineValue(constraint)}.`);
  }

  return parts.join(' ');
}

function collectSchemaFields(document: JsonObject, value: unknown) {
  const root = resolveLocalReference(document, value);
  if (!root) return [];

  const schemas = [root];
  for (const member of asArray(root.allOf) ?? []) {
    const resolved = resolveLocalReference(document, member);
    if (resolved) schemas.push(resolved);
  }

  const properties = new Map<string, unknown>();
  const required = new Set<string>();
  for (const schema of schemas) {
    for (const name of asArray(schema.required) ?? []) {
      if (typeof name === 'string') required.add(name);
    }
    for (const [name, property] of Object.entries(asObject(schema.properties) ?? {})) {
      properties.set(name, property);
    }
  }

  return [...properties].map(([name, property]) => ({
    name,
    required: required.has(name),
    type: schemaType(document, property),
    description: constraintDescription(document, property),
  }));
}

function renderSchemaFields(document: JsonObject, schema: unknown) {
  const fields = collectSchemaFields(document, schema);
  if (!fields.length) return [];

  return [
    '',
    '| Field | Type | Required | Description |',
    '| --- | --- | --- | --- |',
    ...fields.map(
      (field) =>
        `| \`${escapeTableCell(field.name)}\` | \`${escapeTableCell(field.type)}\` | ${
          field.required ? 'Yes' : 'No'
        } | ${escapeTableCell(field.description)} |`,
    ),
  ];
}

function renderParameters(document: JsonObject, pathItem: JsonObject, operation: JsonObject) {
  const parameters = new Map<string, JsonObject>();
  for (const candidate of [
    ...(asArray(pathItem.parameters) ?? []),
    ...(asArray(operation.parameters) ?? []),
  ]) {
    const parameter = resolveLocalReference(document, candidate);
    const name = asString(parameter?.name);
    const location = asString(parameter?.in);
    if (parameter && name && location) parameters.set(`${location}:${name}`, parameter);
  }

  if (!parameters.size) return [];

  return [
    '### Parameters',
    '',
    '| Name | In | Type | Required | Description |',
    '| --- | --- | --- | --- | --- |',
    ...[...parameters.values()].map((parameter) => {
      const schema = parameter.schema;
      const details = [
        asString(parameter.description) ?? constraintDescription(document, schema),
      ];
      const example = parameter.example ?? asObject(schema)?.example;
      if (example !== undefined) details.push(`Example: ${inlineValue(example)}.`);
      if (parameter.deprecated === true) details.push('Deprecated.');

      return `| \`${escapeTableCell(asString(parameter.name) ?? '')}\` | ${escapeTableCell(
        asString(parameter.in) ?? '',
      )} | \`${escapeTableCell(schemaType(document, schema))}\` | ${
        parameter.required === true ? 'Yes' : 'No'
      } | ${escapeTableCell(details.filter(Boolean).join(' '))} |`;
    }),
    '',
  ];
}

function describeSecurityScheme(document: JsonObject, name: string, scopes: unknown) {
  const schemes = asObject(asObject(document.components)?.securitySchemes);
  const scheme = resolveLocalReference(document, schemes?.[name]);
  if (!scheme) return `\`${name}\``;

  const type = asString(scheme.type);
  let description = `\`${name}\``;
  if (type === 'apiKey') {
    description += `: API key in ${asString(scheme.in) ?? 'request'} \`${
      asString(scheme.name) ?? name
    }\``;
  } else if (type === 'http') {
    const httpScheme = asString(scheme.scheme) ?? 'HTTP';
    const bearerFormat = asString(scheme.bearerFormat);
    description += `: ${httpScheme}${bearerFormat ? ` (${bearerFormat})` : ''} authentication`;
  } else if (type === 'oauth2') {
    description += ': OAuth 2.0';
  } else if (type === 'openIdConnect') {
    description += ': OpenID Connect';
  } else if (type === 'mutualTLS') {
    description += ': mutual TLS';
  }

  const scopeNames = (asArray(scopes) ?? []).filter(
    (scope): scope is string => typeof scope === 'string',
  );
  if (scopeNames.length) description += `; scopes: ${scopeNames.map((scope) => `\`${scope}\``).join(', ')}`;
  return description;
}

function renderAuthentication(document: JsonObject, operation: JsonObject) {
  const declaredSecurity = operation.security ?? document.security;
  const requirements = asArray(declaredSecurity);
  if (!requirements) return [];
  if (!requirements.length) return ['### Authentication', '', 'No authentication required.', ''];

  const alternatives = requirements.map((requirement) => {
    const entries = Object.entries(asObject(requirement) ?? {});
    if (!entries.length) return 'No authentication';
    return entries
      .map(([name, scopes]) => describeSecurityScheme(document, name, scopes))
      .join(' **and** ');
  });

  return [
    '### Authentication',
    '',
    alternatives.length > 1 ? 'Provide one of:' : 'Required:',
    '',
    ...alternatives.map((alternative) => `- ${alternative}`),
    '',
  ];
}

function exampleFromMedia(document: JsonObject, media: JsonObject, schema: unknown) {
  if (media.example !== undefined) return media.example;

  for (const candidate of Object.values(asObject(media.examples) ?? {})) {
    const example = resolveLocalReference(document, candidate);
    if (example?.value !== undefined) return example.value;
  }

  const resolvedSchema = resolveLocalReference(document, schema);
  return resolvedSchema?.example;
}

function codeFence(value: unknown, language: string) {
  const content =
    typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? String(value);
  const fence = content.includes('```') ? '````' : '```';
  return [`${fence}${language}`, content, fence];
}

function mediaLanguage(mediaType: string) {
  if (mediaType.includes('json')) return 'json';
  if (mediaType.includes('xml')) return 'xml';
  if (mediaType.includes('yaml')) return 'yaml';
  if (mediaType.includes('event-stream')) return 'text';
  return 'text';
}

function renderContent(document: JsonObject, value: unknown, headingLevel: number) {
  const content = asObject(value);
  if (!content || !Object.keys(content).length) return [];

  const lines: string[] = [];
  for (const [mediaType, mediaValue] of Object.entries(content)) {
    const media = asObject(mediaValue) ?? {};
    lines.push(`${'#'.repeat(headingLevel)} \`${mediaType}\``, '');
    if (media.schema) {
      lines.push(`Schema: ${schemaSummary(document, media.schema)}`);
      lines.push(...renderSchemaFields(document, media.schema));
    }

    const example = exampleFromMedia(document, media, media.schema);
    if (example !== undefined) {
      lines.push('', 'Example:', '', ...codeFence(example, mediaLanguage(mediaType)));
    }
    lines.push('');
  }
  return lines;
}

function renderRequestBody(document: JsonObject, operation: JsonObject) {
  const body = resolveLocalReference(document, operation.requestBody);
  if (!body) return [];

  const lines = [
    '### Request body',
    '',
    `Required: **${body.required === true ? 'yes' : 'no'}**.`,
  ];
  const description = asString(body.description);
  if (description) lines.push('', description);
  lines.push('', ...renderContent(document, body.content, 4));
  return lines;
}

function renderResponseHeaders(document: JsonObject, value: unknown) {
  const headers = asObject(value);
  if (!headers || !Object.keys(headers).length) return [];

  return [
    '',
    '| Response header | Type | Description |',
    '| --- | --- | --- |',
    ...Object.entries(headers).map(([name, headerValue]) => {
      const header = resolveLocalReference(document, headerValue) ?? {};
      return `| \`${escapeTableCell(name)}\` | \`${escapeTableCell(
        schemaType(document, header.schema),
      )}\` | ${escapeTableCell(asString(header.description) ?? '')} |`;
    }),
  ];
}

function responseSortKey(value: string) {
  if (/^\d+$/.test(value)) return Number(value);
  return Number.MAX_SAFE_INTEGER;
}

function renderResponses(document: JsonObject, operation: JsonObject) {
  const responses = asObject(operation.responses);
  if (!responses) return [];

  const entries = Object.entries(responses).sort(
    ([left], [right]) => responseSortKey(left) - responseSortKey(right) || left.localeCompare(right),
  );
  const lines = ['### Responses', ''];

  for (const [status, responseValue] of entries) {
    const response = resolveLocalReference(document, responseValue) ?? {};
    lines.push(`#### \`${status}\``, '');
    const description = asString(response.description);
    if (description) lines.push(description);
    lines.push(...renderResponseHeaders(document, response.headers));

    const content = renderContent(document, response.content, 5);
    if (content.length) lines.push('', ...content);
    else lines.push('', 'No response body.', '');
  }

  return lines;
}

function renderCodeSamples(operation: JsonObject) {
  const samples = asArray(operation['x-codeSamples']) ?? asArray(operation['x-code-samples']);
  if (!samples?.length) return [];

  const lines = ['### Code examples', ''];
  for (const [index, sampleValue] of samples.entries()) {
    const sample = asObject(sampleValue);
    const source = asString(sample?.source);
    if (!sample || !source) continue;

    const label = asString(sample.label) ?? asString(sample.id) ?? `Example ${index + 1}`;
    const language = (asString(sample.lang) ?? asString(sample.id) ?? 'text').replace(
      /[^a-zA-Z0-9_+-]/g,
      '',
    );
    lines.push(`#### ${label}`, '', ...codeFence(source, language), '');
  }
  return lines.length > 2 ? lines : [];
}

function renderServers(document: JsonObject, pathItem: JsonObject, operation: JsonObject) {
  const servers =
    asArray(operation.servers) ?? asArray(pathItem.servers) ?? asArray(document.servers) ?? [];
  const urls = servers
    .map((server) => asString(asObject(server)?.url))
    .filter((url): url is string => Boolean(url));

  if (!urls.length) return [];
  return [`Base URL${urls.length > 1 ? 's' : ''}: ${urls.map((url) => `\`${url}\``).join(', ')}`, ''];
}

function renderOperation(
  document: JsonObject,
  path: string,
  method: string,
  pathItem: JsonObject,
  operation: JsonObject,
) {
  const lines = [`## ${method.toUpperCase()} ${path}`, ''];
  if (operation.deprecated === true) lines.push('> **Deprecated:** This endpoint is deprecated.', '');

  const description = asString(operation.description) ?? asString(operation.summary);
  if (description) lines.push(description, '');

  const metadata: string[] = [];
  const operationId = asString(operation.operationId);
  if (operationId) metadata.push(`Operation ID: \`${operationId}\``);
  const tags = (asArray(operation.tags) ?? []).filter(
    (tag): tag is string => typeof tag === 'string',
  );
  if (tags.length) metadata.push(`Tags: ${tags.map((tag) => `\`${tag}\``).join(', ')}`);
  if (metadata.length) lines.push(...metadata, '');

  lines.push(...renderServers(document, pathItem, operation));
  lines.push(...renderAuthentication(document, operation));
  lines.push(...renderParameters(document, pathItem, operation));
  lines.push(...renderRequestBody(document, operation));
  lines.push(...renderResponses(document, operation));
  lines.push(...renderCodeSamples(operation));
  return lines;
}

export function renderOpenAPIPageMarkdown({
  title,
  description,
  pageUrl,
  props,
}: OpenAPIMarkdownOptions) {
  const lines = [
    `# ${title}`,
    '',
    `HTML documentation: [${pageUrl}](${pageUrl})`,
    '',
    `Complete OpenAPI 3.1 schema: [${openAPISpecUrl}](${openAPISpecUrl})`,
    '',
  ];

  if (!('payload' in props)) {
    if (description) lines.push(description, '');
    return lines.join('\n').trimEnd();
  }

  const document = asObject(props.payload.bundled);
  if (!document) {
    if (description) lines.push(description, '');
    return lines.join('\n').trimEnd();
  }

  let renderedOperations = 0;
  for (const item of props.operations ?? []) {
    const path = item.path;
    const method = item.method.toLowerCase();
    const pathItem = asObject(asObject(document.paths)?.[path]);
    const operation = asObject(pathItem?.[method]);
    if (!pathItem || !operation) continue;

    lines.push(...renderOperation(document, path, method, pathItem, operation), '');
    renderedOperations += 1;
  }

  if (!renderedOperations && description) lines.push(description, '');
  return lines.join('\n').trimEnd();
}

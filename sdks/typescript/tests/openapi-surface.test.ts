import { readFile } from "node:fs/promises";

import { describe, expect, it } from "vitest";

interface OpenAPIOperation {
  parameters?: Array<{ name?: string }>;
}

interface OpenAPIDocument {
  paths: Record<string, Record<string, OpenAPIOperation>>;
  components?: {
    schemas?: Record<string, { properties?: Record<string, unknown> }>;
  };
}

const openAPIPath = new URL(
  "../../../website/public/openapi/vma.json",
  import.meta.url,
);

describe("committed public OpenAPI surface", () => {
  it("contains every native route wrapped by the TypeScript SDK", async () => {
    const document = JSON.parse(
      await readFile(openAPIPath, "utf8"),
    ) as OpenAPIDocument;
    const expected: Readonly<Record<string, readonly string[]>> = {
      "/v1/api_keys": ["get", "post"],
      "/v1/api_keys/{key_id}": ["get"],
      "/v1/api_keys/{key_id}/revoke": ["post"],
      "/v1/api_keys/{key_id}/rotate": ["post"],
      "/v1/agents": ["get", "post"],
      "/v1/agents/{agent_id}": ["get", "post"],
      "/v1/agents/{agent_id}/archive": ["post"],
      "/v1/agents/{agent_id}/versions": ["get"],
      "/v1/environments": ["get", "post"],
      "/v1/environments/{environment_id}": ["delete", "get", "post"],
      "/v1/environments/{environment_id}/archive": ["post"],
      "/v1/sessions": ["get", "post"],
      "/v1/sessions/{session_id}": ["delete", "get", "post"],
      "/v1/sessions/{session_id}/archive": ["post"],
      "/v1/sessions/{session_id}/cancel": ["post"],
      "/v1/sessions/{session_id}/resume": ["post"],
      "/v1/sessions/{session_id}/events": ["get", "post"],
      "/v1/sessions/{session_id}/events/stream": ["get"],
      "/v1/sessions/{session_id}/resources": ["get", "post"],
      "/v1/sessions/{session_id}/resources/{resource_id}": [
        "delete",
        "get",
        "post",
      ],
      "/v1/files": ["get", "post"],
      "/v1/files/{file_id}": ["delete", "get"],
      "/v1/files/{file_id}/content": ["get"],
      "/v1/memory_stores": ["get", "post"],
      "/v1/memory_stores/{memory_store_id}": ["delete", "get", "post"],
      "/v1/memory_stores/{memory_store_id}/archive": ["post"],
      "/v1/memory_stores/{memory_store_id}/memories": ["get", "post"],
      "/v1/memory_stores/{memory_store_id}/memories/by_path": ["get"],
      "/v1/memory_stores/{memory_store_id}/memories/{memory_id}": [
        "delete",
        "get",
        "post",
      ],
      "/v1/memory_stores/{memory_store_id}/memories/{memory_id}/versions": [
        "get",
      ],
      "/v1/memory_stores/{memory_store_id}/memories/{memory_id}/versions/{version}":
        ["get"],
      "/v1/memory_stores/{memory_store_id}/memory_versions": ["get"],
      "/v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}":
        ["get"],
      "/v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}/redact":
        ["post"],
      "/v1/skills": ["get", "post"],
      "/v1/skills/{skill_id}": ["delete", "get"],
      "/v1/skills/{skill_id}/versions": ["get", "post"],
      "/v1/skills/{skill_id}/versions/{version}": ["delete", "get"],
      "/v1/skills/{skill_id}/versions/{version}/content": ["get"],
      "/v1/vaults": ["get", "post"],
      "/v1/vaults/{vault_id}": ["delete", "get", "post"],
      "/v1/vaults/{vault_id}/archive": ["post"],
      "/v1/vaults/{vault_id}/model_credentials": ["get", "post"],
      "/v1/vaults/{vault_id}/model_credentials/{credential_id}": [
        "delete",
        "get",
        "post",
      ],
      "/v1/vaults/{vault_id}/model_credentials/{credential_id}/archive": [
        "post",
      ],
      "/v1/model_providers": ["get"],
      "/v1/model_providers/{provider_id}": ["get"],
      "/v1/usage": ["get"],
    };

    for (const [path, methods] of Object.entries(expected)) {
      expect(
        document.paths[path],
        `missing OpenAPI path ${path}`,
      ).toBeDefined();
      for (const method of methods) {
        expect(
          document.paths[path]?.[method],
          `missing OpenAPI operation ${method.toUpperCase()} ${path}`,
        ).toBeDefined();
      }
    }

    const sessionListParameters =
      document.paths["/v1/sessions"]?.get?.parameters ?? [];
    expect(sessionListParameters.map((parameter) => parameter.name)).toContain(
      "memory_store_id",
    );

    const usageParameters = document.paths["/v1/usage"]?.get?.parameters ?? [];
    expect(usageParameters.map((parameter) => parameter.name)).toEqual(
      expect.arrayContaining([
        "limit",
        "page",
        "session_id",
        "metric",
        "occurred_at[gt]",
        "occurred_at[gte]",
        "occurred_at[lt]",
        "occurred_at[lte]",
      ]),
    );

    const schemas = document.components?.schemas ?? {};
    expect(schemas.SessionFundingRequest).toBeDefined();
    expect(schemas.SessionCreateRequest?.properties?.funding).toBeDefined();
    expect(schemas.UsageEntryResponse).toBeDefined();
    expect(schemas.UsagePageResponse).toBeDefined();
  });
});

import { describe, expect, it, vi } from "vitest";

import Votrix from "../src/index.js";

interface RecordedRequest {
  url: URL;
  method: string;
  body: BodyInit | null;
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });
}

function makeClient(): { client: Votrix; requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const fetch = vi.fn(
    async (
      input: string | URL | Request,
      init?: RequestInit,
    ): Promise<Response> => {
      const request = {
        url: new URL(input instanceof Request ? input.url : String(input)),
        method:
          init?.method ?? (input instanceof Request ? input.method : "GET"),
        body: init?.body ?? null,
      };
      requests.push(request);
      if (
        request.method === "GET" &&
        !request.url.pathname.endsWith("by_path")
      ) {
        const listPaths = [
          "/v1/memory_stores",
          "/memories",
          "/versions",
          "/memory_versions",
        ];
        if (listPaths.some((suffix) => request.url.pathname.endsWith(suffix))) {
          return jsonResponse({ data: [], has_more: false, next_page: null });
        }
      }
      return jsonResponse({ id: "memory_result", type: "memory" });
    },
  );

  return {
    client: new Votrix({
      apiKey: "vma_test_secret",
      baseURL: "https://managed-agents.test",
      fetch,
      maxRetries: 0,
    }),
    requests,
  };
}

function jsonBody(request: RecordedRequest): Record<string, unknown> {
  if (typeof request.body !== "string") {
    throw new Error(`Expected JSON body for ${request.method} ${request.url}`);
  }
  return JSON.parse(request.body) as Record<string, unknown>;
}

describe("memory stores", () => {
  it("routes the complete Memory Store, Memory, and version surface", async () => {
    const { client, requests } = makeClient();

    await client.memoryStores.create({
      name: "Account context",
      description: "Durable account preferences",
      metadata: { team: "support" },
    });
    await client.memoryStores.list({
      limit: 20,
      include_archived: true,
      "created_at[gte]": "2026-07-01T00:00:00Z",
    });
    await client.memoryStores.retrieve("store/one");
    await client.memoryStores.update("store/one", {
      name: "Account memory",
      metadata: { team: null },
    });
    await client.memoryStores.archive("store/one");
    await client.memoryStores.delete("store/one");

    await client.memoryStores.memories.create("store/one", {
      path: ["accounts", "acme"],
      content: "ACME prefers email.",
      actor: "agent_1",
      view: "basic",
    });
    await client.memoryStores.memories.list("store/one", {
      path_prefix: "/accounts",
      depth: 2,
      order: "desc",
      order_by: "created_at",
    });
    await client.memoryStores.memories.retrieveByPath("store/one", {
      path: "/accounts/acme",
      view: "full",
    });
    await client.memoryStores.memories.retrieve("memory/one", {
      memory_store_id: "store/one",
      view: "basic",
    });
    await client.memoryStores.memories.update("memory/one", {
      memory_store_id: "store/one",
      content: "ACME prefers chat.",
      precondition: {
        type: "content_sha256",
        content_sha256: "old-sha",
      },
      view: "full",
    });
    await client.memoryStores.memories.delete("memory/one", {
      memory_store_id: "store/one",
      expected_content_sha256: "current-sha",
    });

    await client.memoryStores.memories.versions.list("memory/one", {
      memory_store_id: "store/one",
      limit: 10,
    });
    await client.memoryStores.memories.versions.retrieve(2, {
      memory_store_id: "store/one",
      memory_id: "memory/one",
    });

    await client.memoryStores.memoryVersions.list("store/one", {
      memory_id: "memory/one",
      operation: "modified",
      api_key_id: "key_1",
      session_id: "session_1",
      view: "basic",
      "created_at[lte]": "2026-07-20T00:00:00Z",
    });
    await client.memoryStores.memoryVersions.retrieve("version/one", {
      memory_store_id: "store/one",
      view: "full",
    });
    await client.memoryStores.memoryVersions.redact("version/one", {
      memory_store_id: "store/one",
    });

    expect(requests).toHaveLength(17);
    expect(
      requests.map((request) => [request.method, request.url.pathname]),
    ).toEqual([
      ["POST", "/v1/memory_stores"],
      ["GET", "/v1/memory_stores"],
      ["GET", "/v1/memory_stores/store%2Fone"],
      ["POST", "/v1/memory_stores/store%2Fone"],
      ["POST", "/v1/memory_stores/store%2Fone/archive"],
      ["DELETE", "/v1/memory_stores/store%2Fone"],
      ["POST", "/v1/memory_stores/store%2Fone/memories"],
      ["GET", "/v1/memory_stores/store%2Fone/memories"],
      ["GET", "/v1/memory_stores/store%2Fone/memories/by_path"],
      ["GET", "/v1/memory_stores/store%2Fone/memories/memory%2Fone"],
      ["POST", "/v1/memory_stores/store%2Fone/memories/memory%2Fone"],
      ["DELETE", "/v1/memory_stores/store%2Fone/memories/memory%2Fone"],
      ["GET", "/v1/memory_stores/store%2Fone/memories/memory%2Fone/versions"],
      ["GET", "/v1/memory_stores/store%2Fone/memories/memory%2Fone/versions/2"],
      ["GET", "/v1/memory_stores/store%2Fone/memory_versions"],
      ["GET", "/v1/memory_stores/store%2Fone/memory_versions/version%2Fone"],
      [
        "POST",
        "/v1/memory_stores/store%2Fone/memory_versions/version%2Fone/redact",
      ],
    ]);

    expect(jsonBody(requests[0]!)).toEqual({
      name: "Account context",
      description: "Durable account preferences",
      metadata: { team: "support" },
    });
    expect(requests[1]!.url.searchParams.get("include_archived")).toBe("true");
    expect(requests[1]!.url.searchParams.get("created_at[gte]")).toBe(
      "2026-07-01T00:00:00Z",
    );
    expect(jsonBody(requests[3]!)).toEqual({
      name: "Account memory",
      metadata: { team: null },
    });

    expect(jsonBody(requests[6]!)).toEqual({
      path: ["accounts", "acme"],
      content: "ACME prefers email.",
      actor: "agent_1",
    });
    expect(requests[6]!.url.searchParams.get("view")).toBe("basic");
    expect(requests[8]!.url.searchParams.get("path")).toBe("/accounts/acme");
    expect(requests[9]!.url.searchParams.get("view")).toBe("basic");
    expect(jsonBody(requests[10]!)).toEqual({
      content: "ACME prefers chat.",
      precondition: {
        type: "content_sha256",
        content_sha256: "old-sha",
      },
    });
    expect(jsonBody(requests[10]!)).not.toHaveProperty("memory_store_id");
    expect(jsonBody(requests[10]!)).not.toHaveProperty("view");
    expect(requests[11]!.url.searchParams.get("expected_content_sha256")).toBe(
      "current-sha",
    );
    expect(requests[12]!.url.searchParams.get("limit")).toBe("10");
    expect(requests[14]!.url.searchParams.get("operation")).toBe("modified");
    expect(requests[14]!.url.searchParams.get("api_key_id")).toBe("key_1");
    expect(requests[15]!.url.searchParams.get("view")).toBe("full");
  });

  it("rejects empty path identifiers before sending a request", () => {
    const { client, requests } = makeClient();
    expect(() => client.memoryStores.retrieve("")).toThrow(
      "Expected a non-empty memoryStoreID",
    );
    expect(() =>
      client.memoryStores.memories.retrieve("", {
        memory_store_id: "store_1",
      }),
    ).toThrow("Expected a non-empty memoryID");
    expect(requests).toHaveLength(0);
  });
});

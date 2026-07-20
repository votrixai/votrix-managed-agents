import { describe, expect, it, vi } from "vitest";

import Votrix, {
  type Session,
  type SessionFundingBinding,
  type UsageEntry,
} from "../src/index.js";

interface RecordedRequest {
  url: URL;
  method: string;
  headers: Headers;
  body: BodyInit | null;
}

type Responder = (
  request: RecordedRequest,
  index: number,
) => Response | Promise<Response>;

function makeClient(responder: Responder): {
  client: Votrix;
  requests: RecordedRequest[];
} {
  const requests: RecordedRequest[] = [];
  const fetch = vi.fn(
    async (
      input: string | URL | Request,
      init?: RequestInit,
    ): Promise<Response> => {
      const headers = new Headers(
        input instanceof Request ? input.headers : undefined,
      );
      new Headers(init?.headers).forEach((value, name) =>
        headers.set(name, value),
      );
      const request: RecordedRequest = {
        url: new URL(input instanceof Request ? input.url : String(input)),
        method:
          init?.method ?? (input instanceof Request ? input.method : "GET"),
        headers,
        body: init?.body ?? null,
      };
      requests.push(request);
      return await responder(request, requests.length - 1);
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

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function listResponse(
  data: unknown[],
  options: {
    hasMore?: boolean;
    firstID?: string | null;
    lastID?: string | null;
    nextPage?: string | null;
  } = {},
): Response {
  return jsonResponse({
    data,
    has_more: options.hasMore ?? false,
    first_id: options.firstID ?? itemID(data[0]),
    last_id: options.lastID ?? itemID(data.at(-1)),
    next_page: options.nextPage ?? null,
  });
}

function itemID(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("id" in value))
    return null;
  return typeof value.id === "string" ? value.id : null;
}

function requestAt(
  requests: RecordedRequest[],
  index: number,
): RecordedRequest {
  const request = requests[index];
  if (!request) throw new Error(`Missing recorded request ${index}`);
  return request;
}

function jsonBody(request: RecordedRequest): Record<string, unknown> {
  if (typeof request.body !== "string") {
    throw new Error(
      `Expected a JSON body for ${request.method} ${request.url.pathname}`,
    );
  }
  return JSON.parse(request.body) as Record<string, unknown>;
}

function apiKeyResponse(secret?: string): Record<string, unknown> {
  return {
    id: "key_1",
    type: "api_key",
    organization_id: "org_test",
    name: "CI",
    prefix: "vma_test_key_1",
    scopes: ["api"],
    expires_at: null,
    created_by: "key_admin",
    metadata: {},
    last_used_at: null,
    revoked_at: null,
    revoked_by: null,
    revocation_reason: null,
    replaced_by_key_id: null,
    replaces_key_id: null,
    created_at: "2026-07-16T00:00:00Z",
    updated_at: "2026-07-16T00:00:00Z",
    secret,
    unexpected_secret_material: "must-not-escape",
  };
}

describe("public resources", () => {
  it("exposes camelCase GA resources and omits deferred capabilities", () => {
    const { client, requests } = makeClient(() => jsonResponse({}));

    expect(client.apiKeys).toBeDefined();
    expect(client.agents).toBeDefined();
    expect(client.environments).toBeDefined();
    expect(client.sessions).toBeDefined();
    expect(client.files).toBeDefined();
    expect(client.skills).toBeDefined();
    expect(client.vaults).toBeDefined();
    expect(client.modelProviders).toBeDefined();
    expect(client.usage).toBeDefined();
    expect(client.agents.versions).toBeDefined();
    expect(client.sessions.events).toBeDefined();
    expect(client.sessions.resources).toBeDefined();
    expect(client.skills.versions).toBeDefined();
    expect(client.vaults.modelCredentials).toBeDefined();
    expect(client.files.retrieveMetadata).toBeTypeOf("function");

    const roots = client as unknown as Record<string, unknown>;
    for (const deferred of [
      "deployments",
      "deploymentRuns",
      "memoryStores",
      "userProfiles",
      "webhooks",
      "billing",
    ]) {
      expect(roots[deferred]).toBeUndefined();
    }
    expect(roots.api_keys).toBeUndefined();
    expect(roots.model_providers).toBeUndefined();
    expect(
      (client.environments as unknown as Record<string, unknown>).work,
    ).toBeUndefined();
    expect(
      (client.sessions as unknown as Record<string, unknown>).threads,
    ).toBeUndefined();
    expect(
      (client.vaults as unknown as Record<string, unknown>).credentials,
    ).toBeUndefined();
    expect(requests).toHaveLength(0);
  });

  it("routes every root resource and public subresource with wire-format payloads", async () => {
    const responses: unknown[] = [
      apiKeyResponse("vma_test_once"),
      { id: "agt_created", type: "agent" },
      { id: "agt_retrieved", type: "agent" },
      {
        data: [{ id: "agt_version", type: "agent" }],
        has_more: false,
        first_id: "agt_version",
        last_id: "agt_version",
        next_page: null,
      },
      { id: "env_1", type: "environment" },
      { id: "sess_1", type: "session" },
      { data: [] },
      { id: "res_1", type: "file", file_id: "file_1" },
      { id: "file_1", type: "file" },
      { id: "skill_1", type: "skill", source: "custom" },
      { id: "skv_1", type: "skill_version", version: "1" },
      { id: "vault_1", type: "vault" },
      {
        id: "cred_1",
        type: "model_credential",
        vault_id: "vault/one",
        model_provider: "anthropic",
        display_name: "Claude",
        metadata: {},
        api_key: "provider-secret-must-not-escape",
        auth: { token: "also-secret" },
      },
      { id: "anthropic/custom", type: "model_provider" },
    ];
    const { client, requests } = makeClient((_request, index) =>
      jsonResponse(responses[index]),
    );

    const createdKey = await client.apiKeys.create({
      name: "CI",
      scopes: ["api"],
      expires_at: null,
    });
    await client.agents.create({
      name: "Research",
      model: { id: "claude-sonnet-4-6" },
      mcp_servers: [{ type: "url", url: "https://mcp.test" }],
    });
    await client.agents.retrieve("agt/one two", { version: 2 });
    await client.agents.versions.list("agt/versions", { limit: 1 });
    await client.environments.create({
      name: "Cloud",
      config: { type: "cloud" },
      scope: null,
    });
    await client.sessions.create({
      agent: "agt_created",
      environment_id: "env_1",
      vault_ids: ["vault_1"],
    });
    await client.sessions.events.send("sess_1", {
      events: [{ type: "user.message", content: "hello" }],
    });
    await client.sessions.resources.add("sess_1", {
      file_id: "file_1",
      mount_path: "/mnt/session/uploads/input.txt",
    });
    await client.files.upload({
      file: new Uint8Array([1, 2, 3]),
      filename: "input.txt",
      mime_type: "text/plain",
    });
    await client.skills.create({
      display_title: "Tools",
      files: [
        {
          filename: "tools/SKILL.md",
          content: "---\nname: tools\ndescription: Tools\n---",
        },
      ],
    });
    await client.skills.versions.create("skill/one", {
      files: [
        {
          filename: "tools/SKILL.md",
          content: "---\nname: tools\ndescription: Updated\n---",
        },
      ],
    });
    await client.vaults.create({ display_name: "Providers" });
    const credential = await client.vaults.modelCredentials.create(
      "vault/one",
      {
        provider: "anthropic",
        api_key: "provider-secret",
        display_name: "Claude",
      },
    );
    await client.modelProviders.retrieve("anthropic/custom");

    expect(requests).toHaveLength(14);
    expect(createdKey.secret).toBe("vma_test_once");
    expect(createdKey).not.toHaveProperty("unexpected_secret_material");
    expect(credential).not.toHaveProperty("api_key");
    expect(credential).not.toHaveProperty("auth");

    expect(requestAt(requests, 0).method).toBe("POST");
    expect(requestAt(requests, 0).url.pathname).toBe("/v1/api_keys");
    expect(jsonBody(requestAt(requests, 0))).toMatchObject({
      name: "CI",
      scopes: ["api"],
      expires_at: null,
    });

    const agentCreate = requestAt(requests, 1);
    expect(agentCreate.url.pathname).toBe("/v1/agents");
    expect(jsonBody(agentCreate)).toMatchObject({
      model: { id: "claude-sonnet-4-6" },
      mcp_servers: [{ type: "url", url: "https://mcp.test" }],
    });
    expect(jsonBody(agentCreate)).not.toHaveProperty("mcpServers");

    const agentRetrieve = requestAt(requests, 2);
    expect(agentRetrieve.url.pathname).toBe("/v1/agents/agt%2Fone%20two");
    expect(agentRetrieve.url.searchParams.get("version")).toBe("2");
    expect(requestAt(requests, 3).url.pathname).toBe(
      "/v1/agents/agt%2Fversions/versions",
    );
    expect(requestAt(requests, 3).url.searchParams.get("limit")).toBe("1");

    expect(requestAt(requests, 4).url.pathname).toBe("/v1/environments");
    expect(jsonBody(requestAt(requests, 4))).toEqual({
      name: "Cloud",
      config: { type: "cloud" },
      scope: null,
    });

    const sessionCreate = requestAt(requests, 5);
    expect(sessionCreate.url.pathname).toBe("/v1/sessions");
    expect(jsonBody(sessionCreate)).toMatchObject({
      environment_id: "env_1",
      vault_ids: ["vault_1"],
    });
    expect(jsonBody(sessionCreate)).not.toHaveProperty("environmentID");

    expect(requestAt(requests, 6).url.pathname).toBe(
      "/v1/sessions/sess_1/events",
    );
    expect(jsonBody(requestAt(requests, 6))).toEqual({
      events: [{ type: "user.message", content: "hello" }],
    });
    expect(requestAt(requests, 7).url.pathname).toBe(
      "/v1/sessions/sess_1/resources",
    );
    expect(jsonBody(requestAt(requests, 7))).toEqual({
      file_id: "file_1",
      mount_path: "/mnt/session/uploads/input.txt",
      type: "file",
    });

    const fileUpload = requestAt(requests, 8);
    expect(fileUpload.url.pathname).toBe("/v1/files");
    expect(fileUpload.body).toBeInstanceOf(FormData);
    const uploadedFile = (fileUpload.body as FormData).get("file");
    expect(uploadedFile).toBeInstanceOf(File);
    expect((uploadedFile as File).name).toBe("input.txt");
    expect((uploadedFile as File).type).toBe("text/plain");

    expect(requestAt(requests, 9).url.pathname).toBe("/v1/skills");
    expect(jsonBody(requestAt(requests, 9))).toMatchObject({
      display_title: "Tools",
      files: [{ filename: "tools/SKILL.md" }],
    });
    expect(requestAt(requests, 10).url.pathname).toBe(
      "/v1/skills/skill%2Fone/versions",
    );
    expect(requestAt(requests, 11).url.pathname).toBe("/v1/vaults");
    expect(requestAt(requests, 12).url.pathname).toBe(
      "/v1/vaults/vault%2Fone/model_credentials",
    );
    expect(jsonBody(requestAt(requests, 12))).toMatchObject({
      provider: "anthropic",
      api_key: "provider-secret",
    });
    expect(requestAt(requests, 13).url.pathname).toBe(
      "/v1/model_providers/anthropic%2Fcustom",
    );
  });

  it("serializes array query values as repeated keys and opens the canonical SSE route", async () => {
    const { client, requests } = makeClient((request) => {
      if (request.url.pathname.endsWith("/events/stream")) {
        return new Response(
          'id: 3\nevent: agent.message\ndata: {"id":"evt_3","type":"agent.message","session_id":"sess/one","seq":3}\n\n',
          { headers: { "content-type": "text/event-stream" } },
        );
      }
      return listResponse([]);
    });

    await client.sessions.list({ statuses: ["idle", "running"] });
    const stream = await client.sessions.events.stream("sess/one", {
      after_seq: 2,
      event_deltas: ["agent.message", "agent.thinking"],
      last_event_id: "2",
      max_reconnects: 0,
    });
    const events = [];
    for await (const event of stream) events.push(event);

    expect(requestAt(requests, 0).url.searchParams.getAll("statuses")).toEqual([
      "idle",
      "running",
    ]);
    const streamRequest = requestAt(requests, 1);
    expect(streamRequest.url.pathname).toBe(
      "/v1/sessions/sess%2Fone/events/stream",
    );
    expect(streamRequest.url.searchParams.get("after_seq")).toBe("2");
    expect(streamRequest.url.searchParams.getAll("event_deltas")).toEqual([
      "agent.message",
      "agent.thinking",
    ]);
    expect(streamRequest.headers.get("last-event-id")).toBe("2");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      id: "evt_3",
      type: "agent.message",
      sse_id: "3",
      seq: 3,
    });
  });

  it("automatically adds idempotency keys and reuses caller-provided keys", async () => {
    const { client, requests } = makeClient((request) =>
      request.url.pathname.endsWith("/events")
        ? jsonResponse({ data: [] })
        : jsonResponse({ id: "sess_1", type: "session" }),
    );
    const sessionParams = {
      agent: "agt_1",
      environment_id: "env_1",
    } as const;
    const eventParams = {
      events: [{ type: "user.message", content: "hello" }],
    } as const;

    await client.sessions.create(sessionParams);
    await client.sessions.events.send("sess_1", eventParams);
    const callerOptions = { idempotencyKey: "caller-idempotency-key" };
    await client.sessions.create(sessionParams, callerOptions);
    await client.sessions.create(sessionParams, callerOptions);
    await client.sessions.events.send("sess_1", eventParams, callerOptions);
    await client.sessions.events.send("sess_1", eventParams, callerOptions);

    const automaticKeys = requests
      .slice(0, 2)
      .map((request) => request.headers.get("idempotency-key"));
    expect(automaticKeys[0]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(automaticKeys[1]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(automaticKeys[0]).not.toBe(automaticKeys[1]);
    expect(
      requests
        .slice(2)
        .map((request) => request.headers.get("idempotency-key")),
    ).toEqual(Array(4).fill("caller-idempotency-key"));
  });

  it("sends explicit Session funding and types its immutable response binding", async () => {
    const sessionPayload = {
      id: "sess_funded",
      type: "session",
      status_details: {
        model_credential_binding: {
          version: 1,
          source: "platform",
          credential_id: null,
          vault_id: null,
          model_provider: "openrouter",
        },
      },
    };
    const { client, requests } = makeClient((request) => {
      if (request.method === "GET" && request.url.pathname === "/v1/sessions") {
        return listResponse([sessionPayload]);
      }
      return jsonResponse(
        sessionPayload,
        request.method === "POST" ? 201 : 200,
      );
    });

    const created: Session = await client.sessions.create({
      agent: "agt_funded",
      environment_id: "env_funded",
      funding: { type: "platform_credits" },
    });
    const retrieved: Session = await client.sessions.retrieve(created.id);
    const listed = await client.sessions.list({ limit: 1 });

    const bindings: Array<SessionFundingBinding | null | undefined> = [
      created.status_details.model_credential_binding,
      retrieved.status_details.model_credential_binding,
      listed.data[0]?.status_details.model_credential_binding,
    ];
    expect(bindings.map((binding) => binding?.source)).toEqual([
      "platform",
      "platform",
      "platform",
    ]);
    expect(jsonBody(requestAt(requests, 0))).toEqual({
      agent: "agt_funded",
      environment_id: "env_funded",
      funding: { type: "platform_credits" },
    });
    expect(requestAt(requests, 1).url.pathname).toBe(
      "/v1/sessions/sess_funded",
    );
    expect(requestAt(requests, 2).url.searchParams.get("limit")).toBe("1");
  });

  it("lists raw usage with opaque pagination and exact filters", async () => {
    const usagePayload = (id: string, quantity: number): UsageEntry => ({
      id,
      type: "usage",
      organization_id: "org_sdk",
      metric: "model_tokens",
      quantity,
      unit: "token",
      provider: "openrouter",
      model: "deepseek/deepseek-v4-pro",
      source_type: "session",
      source_id: "sess_sdk",
      dimensions: { input_tokens: quantity },
      data: { accounting_phase: "postflight_actual" },
      occurred_at: "2026-07-16T14:00:00Z",
      future_usage_field: "preserved",
    });
    const { client, requests } = makeClient((request) => {
      const page = request.url.searchParams.get("page");
      if (page === null) {
        return listResponse([usagePayload("usage_2", 20)], {
          hasMore: true,
          nextPage: "usage_opaque_next",
        });
      }
      expect(page).toBe("usage_opaque_next");
      return listResponse([usagePayload("usage_1", 10)]);
    });

    const page = await client.usage.list({
      limit: 1,
      session_id: "sess_sdk",
      metric: "model_tokens",
      "occurred_at[gt]": "2026-07-16T10:00:00Z",
      "occurred_at[gte]": "2026-07-16T11:00:00Z",
      "occurred_at[lt]": "2026-07-17T00:00:00Z",
      "occurred_at[lte]": "2026-07-17T01:00:00Z",
    });
    expect(page.data[0]).toMatchObject({
      id: "usage_2",
      quantity: 20,
      future_usage_field: "preserved",
    });
    const nextPage = await page.getNextPage();
    expect(nextPage.data[0]?.id).toBe("usage_1");
    expect(nextPage.hasNextPage()).toBe(false);

    expect(requests.map((request) => request.url.pathname)).toEqual([
      "/v1/usage",
      "/v1/usage",
    ]);
    expect(Object.fromEntries(requestAt(requests, 0).url.searchParams)).toEqual(
      {
        limit: "1",
        session_id: "sess_sdk",
        metric: "model_tokens",
        "occurred_at[gt]": "2026-07-16T10:00:00Z",
        "occurred_at[gte]": "2026-07-16T11:00:00Z",
        "occurred_at[lt]": "2026-07-17T00:00:00Z",
        "occurred_at[lte]": "2026-07-17T01:00:00Z",
      },
    );
    expect(requestAt(requests, 1).url.searchParams.get("page")).toBe(
      "usage_opaque_next",
    );
  });

  it("keeps raw page responses readable and preserves distinct resource versions", async () => {
    const { client, requests } = makeClient((request) => {
      if (request.url.pathname === "/v1/model_providers") {
        return listResponse([
          {
            id: "anthropic",
            type: "model_provider",
            display_name: "Anthropic",
          },
        ]);
      }
      if (request.url.pathname === "/v1/agents") {
        if (request.url.searchParams.get("page") === "page_loop") {
          return listResponse(
            [
              { id: "agt_shared", type: "agent" },
              { id: "agt_second", type: "agent" },
            ],
            {
              hasMore: true,
              firstID: "agt_shared",
              lastID: "agt_second",
              nextPage: "page_loop",
            },
          );
        }
        return listResponse([{ id: "agt_shared", type: "agent" }], {
          hasMore: true,
          firstID: "agt_shared",
          lastID: "agt_shared",
          nextPage: "page_loop",
        });
      }
      if (request.url.pathname === "/v1/agents/agt_versions/versions") {
        return listResponse([
          { id: "agt_versions", type: "agent", version: 1 },
          { id: "agt_versions", type: "agent", version: 2 },
        ]);
      }
      throw new Error(`Unexpected request: ${request.url}`);
    });

    const rawResponse = await client.modelProviders.list().asResponse();
    expect(rawResponse.bodyUsed).toBe(false);
    await expect(rawResponse.json()).resolves.toMatchObject({
      data: [{ id: "anthropic" }],
    });

    const awaitedPage = await client.modelProviders.list();
    expect(awaitedPage.data.map((provider) => provider.id)).toEqual([
      "anthropic",
    ]);
    expect(awaitedPage.hasNextPage()).toBe(false);

    const iteratedIDs: string[] = [];
    for await (const agent of client.agents.list({ limit: 2 })) {
      iteratedIDs.push(agent.id);
    }
    expect(iteratedIDs).toEqual(["agt_shared", "agt_second"]);

    const versions: number[] = [];
    for await (const version of client.agents.versions.list("agt_versions")) {
      versions.push(version.version);
    }
    expect(versions).toEqual([1, 2]);

    const agentRequests = requests.filter(
      (request) => request.url.pathname === "/v1/agents",
    );
    expect(agentRequests).toHaveLength(2);
    expect(agentRequests[0]?.url.searchParams.get("page")).toBeNull();
    expect(agentRequests[1]?.url.searchParams.get("page")).toBe("page_loop");
  });

  it("advances file pagination in both after_id and before_id directions", async () => {
    const { client, requests } = makeClient((request) => {
      const afterID = request.url.searchParams.get("after_id");
      const beforeID = request.url.searchParams.get("before_id");
      if (afterID === "file_start") {
        return listResponse([{ id: "file_mid", type: "file" }], {
          hasMore: true,
          firstID: "file_mid",
          lastID: "file_mid",
        });
      }
      if (afterID === "file_mid") {
        return listResponse([{ id: "file_end", type: "file" }]);
      }
      if (beforeID === "file_end") {
        return listResponse([{ id: "file_mid_before", type: "file" }], {
          hasMore: true,
          firstID: "file_mid_before",
          lastID: "file_mid_before",
        });
      }
      if (beforeID === "file_mid_before") {
        return listResponse([{ id: "file_start", type: "file" }]);
      }
      throw new Error(`Unexpected file cursor: ${request.url}`);
    });

    const afterPage = await client.files.list({ after_id: "file_start" });
    const afterNext = await afterPage.getNextPage();
    expect(afterNext.data.map((file) => file.id)).toEqual(["file_end"]);

    const beforePage = await client.files.list({ before_id: "file_end" });
    const beforeNext = await beforePage.getNextPage();
    expect(beforeNext.data.map((file) => file.id)).toEqual(["file_start"]);

    expect(requests.map((request) => request.url.search)).toEqual([
      "?after_id=file_start",
      "?after_id=file_mid",
      "?before_id=file_end",
      "?before_id=file_mid_before",
    ]);
  });
});

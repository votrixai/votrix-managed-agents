import { afterEach, describe, expect, it, vi } from "vitest";

import Votrix, {
  APIConnectionError,
  APITimeoutError,
  BadRequestError,
  InternalServerError,
  RateLimitError,
  UnprocessableEntityError,
  VotrixError,
  type Fetch,
} from "../src/index.js";

const configurationEnvironmentNames = [
  "VMA_API_KEY",
  "VOTRIX_VMA_API_KEY",
  "VMA_BASE_URL",
  "VOTRIX_VMA_BASE_URL",
  "VOTRIX_API_KEY",
  "VOTRIX_BASE_URL",
] as const;
const originalConfigurationEnvironment = new Map(
  configurationEnvironmentNames.map((name) => [name, process.env[name]]),
);

afterEach(() => {
  for (const name of configurationEnvironmentNames) {
    restoreEnvironment(name, originalConfigurationEnvironment.get(name));
  }
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("client configuration and transport", () => {
  it("requires configuration and can read the client environment variables", async () => {
    clearConfigurationEnvironment();

    expect(() => new Votrix({ fetch: successFetch() })).toThrow(
      /apiKey is required/,
    );
    expect(
      () => new Votrix({ apiKey: "vma_test_explicit", fetch: successFetch() }),
    ).toThrow(/baseURL is required/);

    process.env.VMA_API_KEY = "vma_test_from_environment";
    process.env.VMA_BASE_URL = "https://environment.votrix.test";
    const requests: URL[] = [];
    const fetcher: Fetch = async (input) => {
      requests.push(requestURL(input));
      return jsonResponse(providerPayload());
    };
    const client = new Votrix({ fetch: fetcher, maxRetries: 0 });

    await client.modelProviders.retrieve("openrouter");

    expect(client.apiKey).toBe("vma_test_from_environment");
    expect(client.baseURL).toBe("https://environment.votrix.test/");
    expect(requests[0]?.href).toBe(
      "https://environment.votrix.test/v1/model_providers/openrouter",
    );
  });

  it("supports the VOTRIX_VMA environment aliases and matching duplicate values", () => {
    clearConfigurationEnvironment();
    process.env.VOTRIX_VMA_API_KEY = "vma_test_from_alias";
    process.env.VOTRIX_VMA_BASE_URL = "https://alias.votrix.test";

    const aliasClient = new Votrix({ fetch: successFetch() });
    expect(aliasClient.apiKey).toBe("vma_test_from_alias");
    expect(aliasClient.baseURL).toBe("https://alias.votrix.test/");

    process.env.VMA_API_KEY = "vma_test_from_alias";
    process.env.VMA_BASE_URL = "https://alias.votrix.test";
    const matchingClient = new Votrix({ fetch: successFetch() });
    expect(matchingClient.apiKey).toBe("vma_test_from_alias");
    expect(matchingClient.baseURL).toBe("https://alias.votrix.test/");
  });

  it("rejects conflicting VMA environment aliases without exposing their values", () => {
    clearConfigurationEnvironment();
    process.env.VMA_API_KEY = "vma_live_primary_secret";
    process.env.VOTRIX_VMA_API_KEY = "vma_live_alias_secret";
    process.env.VMA_BASE_URL = "https://vma.votrixai.com";

    expect(() => new Votrix({ fetch: successFetch() })).toThrow(
      "VMA_API_KEY and VOTRIX_VMA_API_KEY are both set but have different values; unset one or make them match",
    );
    try {
      new Votrix({ fetch: successFetch() });
    } catch (error) {
      expect(String(error)).not.toContain("primary_secret");
      expect(String(error)).not.toContain("alias_secret");
    }

    delete process.env.VOTRIX_VMA_API_KEY;
    process.env.VOTRIX_VMA_BASE_URL = "https://staging-vma.votrixai.com";
    expect(() => new Votrix({ fetch: successFetch() })).toThrow(
      "VMA_BASE_URL and VOTRIX_VMA_BASE_URL are both set but have different values; unset one or make them match",
    );
  });

  it("gives explicit options priority over conflicting or generic environment variables", () => {
    clearConfigurationEnvironment();
    process.env.VMA_API_KEY = "vma_test_primary";
    process.env.VOTRIX_VMA_API_KEY = "vma_test_alias";
    process.env.VMA_BASE_URL = "https://primary.votrix.test";
    process.env.VOTRIX_VMA_BASE_URL = "https://alias.votrix.test";
    process.env.VOTRIX_API_KEY = "generic-main-product-key";
    process.env.VOTRIX_BASE_URL = "https://api.votrixai.com";

    const client = new Votrix({
      apiKey: "vma_test_explicit",
      baseURL: "https://explicit.votrix.test",
      fetch: successFetch(),
    });
    expect(client.apiKey).toBe("vma_test_explicit");
    expect(client.baseURL).toBe("https://explicit.votrix.test/");

    delete process.env.VMA_API_KEY;
    delete process.env.VOTRIX_VMA_API_KEY;
    expect(() => new Votrix({ fetch: successFetch() })).toThrow(
      /apiKey is required/,
    );
  });

  it("sends native headers, request overrides, and exactly one auth scheme", async () => {
    const observed: Headers[] = [];
    const fetcher: Fetch = async (_input, init) => {
      observed.push(new Headers(init?.headers));
      return jsonResponse(providerPayload());
    };

    const apiKeyClient = new Votrix({
      apiKey: "vma_test_header_secret",
      baseURL: "https://api.votrix.test/root/",
      beta: "votrix-test-beta",
      defaultHeaders: { "x-default-test": "default" },
      fetch: fetcher,
      maxRetries: 0,
    });
    await apiKeyClient.modelProviders.retrieve("provider/with slash", {
      headers: {
        authorization: "Bearer must-not-leak",
        "x-request-test": "request",
      },
    });

    const bearerClient = new Votrix({
      apiKey: "vma_test_bearer_secret",
      baseURL: "https://api.votrix.test",
      authScheme: "bearer",
      fetch: fetcher,
      maxRetries: 0,
    });
    await bearerClient.modelProviders.retrieve("openrouter", {
      headers: { "x-api-key": "must-not-leak" },
    });

    const apiKeyHeaders = observed[0];
    expect(apiKeyHeaders?.get("x-api-key")).toBe("vma_test_header_secret");
    expect(apiKeyHeaders?.get("authorization")).toBeNull();
    expect(apiKeyHeaders?.get("accept")).toBe("application/json");
    expect(apiKeyHeaders?.get("votrix-managed-agents-beta")).toBe(
      "votrix-test-beta",
    );
    expect(apiKeyHeaders?.get("x-votrix-sdk-version")).toBe("0.1.0");
    expect(apiKeyHeaders?.get("user-agent")).toBe(
      "votrix-managed-agents-typescript/0.1.0",
    );
    expect(apiKeyHeaders?.get("x-default-test")).toBe("default");
    expect(apiKeyHeaders?.get("x-request-test")).toBe("request");

    const bearerHeaders = observed[1];
    expect(bearerHeaders?.get("authorization")).toBe(
      "Bearer vma_test_bearer_secret",
    );
    expect(bearerHeaders?.get("x-api-key")).toBeNull();
  });

  it("provides Anthropic-style asResponse and withResponse helpers", async () => {
    let call = 0;
    const fetcher: Fetch = async () => {
      call += 1;
      return jsonResponse(providerPayload(`provider_${call}`), 200, {
        "x-request-id": `request_${call}`,
        "x-contract": "raw",
      });
    };
    const client = makeClient(fetcher);

    const promise = client.modelProviders.retrieve("first");
    expect(promise).toBeInstanceOf(Promise);
    const response = await promise.asResponse();
    expect(response.status).toBe(200);
    expect(response.headers.get("x-contract")).toBe("raw");
    await expect(response.json()).resolves.toMatchObject({ id: "provider_1" });

    const result = await client.modelProviders
      .retrieve("second")
      .withResponse();
    expect(result.data.id).toBe("provider_2");
    expect(result.response.status).toBe(200);
    expect(result.request_id).toBe("request_2");
    expect(
      (result.data as unknown as Record<string, unknown>)._request_id,
    ).toBe("request_2");
    expect(JSON.stringify(result.data)).not.toContain("_request_id");
  });

  it("honors Retry-After for replay-safe requests", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fetcher: Fetch = async () => {
      calls += 1;
      if (calls === 1) {
        return jsonResponse(
          { error: { type: "overloaded_error", message: "retry" } },
          503,
          { "retry-after": "2" },
        );
      }
      return jsonResponse(providerPayload());
    };
    const client = makeClient(fetcher, { maxRetries: 1 });

    const pending = client.modelProviders.retrieve("openrouter");
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);

    await vi.advanceTimersByTimeAsync(1_999);
    expect(calls).toBe(1);

    await vi.advanceTimersByTimeAsync(1);
    await expect(pending).resolves.toMatchObject({ id: "openrouter" });
    expect(calls).toBe(2);
  });

  it("never retries an unsafe POST without an idempotency key", async () => {
    let calls = 0;
    const fetcher: Fetch = async () => {
      calls += 1;
      return jsonResponse({ detail: "unsafe to replay" }, 503);
    };
    const client = makeClient(fetcher, { maxRetries: 3 });

    await expect(
      client.request("POST", "/v1/unsafe", {
        body: { value: "once" },
        options: { retry: true },
      }),
    ).rejects.toBeInstanceOf(InternalServerError);
    expect(calls).toBe(1);
  });

  it("retries Session creation with one body and one generated idempotency key", async () => {
    const requests: Array<{ body: string | null; key: string | null }> = [];
    const fetcher: Fetch = async (_input, init) => {
      const headers = new Headers(init?.headers);
      requests.push({
        body: typeof init?.body === "string" ? init.body : null,
        key: headers.get("idempotency-key"),
      });
      if (requests.length === 1) {
        return jsonResponse({ detail: "retry Session create" }, 503, {
          "retry-after": "0",
        });
      }
      return jsonResponse({ id: "session_1", type: "session" }, 201);
    };
    const client = makeClient(fetcher, { maxRetries: 1 });

    const session = await client.sessions.create({
      agent: "agent_1",
      environment_id: "environment_1",
    });

    expect(session.id).toBe("session_1");
    expect(requests).toHaveLength(2);
    expect(requests[0]).toEqual(requests[1]);
    expect(requests[0]?.key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(JSON.parse(requests[0]?.body ?? "{}")).toEqual({
      agent: "agent_1",
      environment_id: "environment_1",
    });
  });

  it("maps status errors and exposes request and rate-limit metadata", async () => {
    const fetcher: Fetch = async () =>
      jsonResponse(
        {
          error: {
            type: "rate_limit_error",
            code: "request_quota_exceeded",
            message: "slow down",
          },
        },
        429,
        {
          "x-request-id": "request_rate_limited",
          "retry-after": "3",
          "x-ratelimit-remaining": "0",
        },
      );
    const client = makeClient(fetcher);

    const error = await captureError(
      client.modelProviders.retrieve("openrouter"),
    );
    expect(error).toBeInstanceOf(RateLimitError);
    if (!(error instanceof RateLimitError)) throw error;

    expect(error.statusCode).toBe(429);
    expect(error.errorType).toBe("rate_limit_error");
    expect(error.errorCode).toBe("request_quota_exceeded");
    expect(error.requestID).toBe("request_rate_limited");
    expect(error.retryAfter).toBe("3");
    expect(error.rateLimitHeaders).toEqual({
      "retry-after": "3",
      "x-ratelimit-remaining": "0",
    });
  });

  it("redacts client, request, and response secrets from structured errors", async () => {
    const clientKey = "vma_test_client_secret_value";
    const providerKey = "sk-provider-secret-value";
    const fetcher: Fetch = async () =>
      jsonResponse(
        {
          error: {
            type: "invalid_request_error",
            code: "invalid_model_credential",
            message: `invalid ${providerKey} and ${clientKey}`,
          },
          api_key: providerKey,
          nested: {
            password: "database-password",
            detail: `do not print ${providerKey}`,
          },
        },
        422,
      );
    const client = new Votrix({
      apiKey: clientKey,
      baseURL: "https://api.votrix.test",
      fetch: fetcher,
      maxRetries: 0,
    });

    const error = await captureError(
      client.vaults.modelCredentials.create("vault_1", {
        provider: "openrouter",
        api_key: providerKey,
      }),
    );
    expect(error).toBeInstanceOf(UnprocessableEntityError);
    if (!(error instanceof UnprocessableEntityError)) throw error;

    const rendered = `${String(error)} ${error.stack ?? ""} ${JSON.stringify(error.body)}`;
    expect(rendered).not.toContain(clientKey);
    expect(rendered).not.toContain(providerKey);
    expect(rendered).not.toContain("database-password");
    expect(rendered).toContain("[redacted]");
    expect(error.body).toMatchObject({
      api_key: "[redacted]",
      nested: { password: "[redacted]", detail: "do not print [redacted]" },
    });
  });

  it("sanitizes API key and model Credential objects at runtime", async () => {
    const fetcher: Fetch = async (input, init) => {
      const url = requestURL(input);
      if (url.pathname === "/v1/api_keys/key_1") {
        return jsonResponse({
          ...apiKeyPayload(),
          secret: "must-not-survive-a-read",
          internal_hash: "must-not-survive-a-read",
        });
      }
      if (url.pathname === "/v1/api_keys" && init?.method === "POST") {
        return jsonResponse(
          {
            ...apiKeyPayload(),
            secret: "vma_test_returned_once",
            internal_hash: "must-not-survive-create",
          },
          201,
        );
      }
      return jsonResponse({
        id: "credential_1",
        type: "model_credential",
        vault_id: "vault_1",
        model_provider: "openrouter",
        display_name: "End-user key",
        metadata: {},
        api_key: "must-not-survive-credential",
        auth: { secret_name: "OPENROUTER_API_KEY" },
        secret_name: "OPENROUTER_API_KEY",
        private_provider_config: true,
      });
    };
    const client = makeClient(fetcher);

    const readKey = await client.apiKeys.retrieve("key_1");
    const createdKey = await client.apiKeys.create({ name: "Production" });
    const credential = await client.vaults.modelCredentials.retrieve(
      "credential_1",
      {
        vault_id: "vault_1",
      },
    );

    expect(readKey).not.toHaveProperty("secret");
    expect(readKey).not.toHaveProperty("internal_hash");
    expect(createdKey.secret).toBe("vma_test_returned_once");
    expect(createdKey).not.toHaveProperty("internal_hash");
    expect(credential).not.toHaveProperty("api_key");
    expect(credential).not.toHaveProperty("auth");
    expect(credential).not.toHaveProperty("secret_name");
    expect(credential).not.toHaveProperty("private_provider_config");
  });

  it("classifies client timeouts without retrying when maxRetries is zero", async () => {
    vi.useFakeTimers();
    const fetcher = hangingFetch();
    const client = makeClient(fetcher, { timeout: 25 });

    const observed = captureError(client.modelProviders.retrieve("openrouter"));
    await vi.advanceTimersByTimeAsync(25);

    await expect(observed).resolves.toBeInstanceOf(APITimeoutError);
  });

  it("applies timeout and caller abort while reading a JSON response body", async () => {
    vi.useFakeTimers();
    const timeoutCancelled = vi.fn();
    const timeoutClient = makeClient(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({ cancel: timeoutCancelled }),
          { headers: { "content-type": "application/json" } },
        ),
      { timeout: 25 },
    );

    const timedOut = captureError(
      timeoutClient.modelProviders.retrieve("openrouter"),
    );
    await vi.advanceTimersByTimeAsync(25);
    await expect(timedOut).resolves.toBeInstanceOf(APITimeoutError);
    expect(timeoutCancelled).toHaveBeenCalledTimes(1);

    const abortCancelled = vi.fn();
    const abortClient = makeClient(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({ cancel: abortCancelled }),
          { headers: { "content-type": "application/json" } },
        ),
      { timeout: 0 },
    );
    const controller = new AbortController();
    const aborted = captureError(
      abortClient.modelProviders.retrieve("openrouter", {
        signal: controller.signal,
      }),
    );
    await vi.advanceTimersByTimeAsync(0);
    controller.abort(new Error("stop reading"));
    await expect(aborted).resolves.toBeInstanceOf(APIConnectionError);
    expect(abortCancelled).toHaveBeenCalledTimes(1);
  });

  it("bounds a stalled HTTP error body while preserving its status class", async () => {
    vi.useFakeTimers();
    const cancelled = vi.fn();
    const client = makeClient(
      async () =>
        new Response(new ReadableStream<Uint8Array>({ cancel: cancelled }), {
          status: 400,
          headers: { "content-type": "application/json" },
        }),
      { timeout: 25 },
    );

    const observed = captureError(client.modelProviders.retrieve("openrouter"));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(25);

    await expect(observed).resolves.toBeInstanceOf(BadRequestError);
    expect(cancelled).toHaveBeenCalledTimes(1);
  });

  it("retries a replay-safe request when its JSON body stream fails", async () => {
    let calls = 0;
    const fetcher: Fetch = async () => {
      calls += 1;
      if (calls === 1) {
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.error(new Error("body connection reset"));
            },
          }),
          { headers: { "content-type": "application/json" } },
        );
      }
      return jsonResponse(providerPayload());
    };
    const client = makeClient(fetcher, { maxRetries: 1, timeout: 0 });

    await expect(
      client.modelProviders.retrieve("openrouter"),
    ).resolves.toMatchObject({ id: "openrouter" });
    expect(calls).toBe(2);
  });

  it("shares one retry budget across HTTP status and body failures", async () => {
    let calls = 0;
    const fetcher: Fetch = async () => {
      calls += 1;
      if (calls === 1) {
        return jsonResponse({ detail: "retry once" }, 503, {
          "retry-after": "0",
        });
      }
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.error(new Error("body reset after status retry"));
          },
        }),
        { headers: { "content-type": "application/json" } },
      );
    };
    const client = makeClient(fetcher, { maxRetries: 1, timeout: 0 });

    await expect(
      client.modelProviders.retrieve("openrouter"),
    ).rejects.toBeInstanceOf(APIConnectionError);
    expect(calls).toBe(2);
  });

  it("propagates caller aborts as connection errors without retrying", async () => {
    const fetcher = vi.fn(hangingFetch());
    const client = makeClient(fetcher, { maxRetries: 3, timeout: 0 });
    const controller = new AbortController();

    const observed = captureError(
      client.modelProviders.retrieve("openrouter", {
        signal: controller.signal,
      }),
    );
    controller.abort(new Error("caller cancelled"));

    const error = await observed;
    expect(error).toBeInstanceOf(APIConnectionError);
    expect(error).not.toBeInstanceOf(APITimeoutError);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("rejects new work after close without invoking fetch", async () => {
    const fetcher = vi.fn(successFetch());
    const client = makeClient(fetcher);
    client.close();

    await expect(
      client.modelProviders.retrieve("openrouter"),
    ).rejects.toBeInstanceOf(VotrixError);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

function makeClient(
  fetcher: Fetch,
  overrides: { timeout?: number; maxRetries?: number } = {},
): Votrix {
  return new Votrix({
    apiKey: "vma_test_secret",
    baseURL: "https://api.votrix.test",
    fetch: fetcher,
    timeout: overrides.timeout ?? 60_000,
    maxRetries: overrides.maxRetries ?? 0,
  });
}

function successFetch(): Fetch {
  return async () => jsonResponse(providerPayload());
}

function hangingFetch(): Fetch {
  return async (_input, init) =>
    await new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) {
        reject(new Error("Expected an AbortSignal"));
        return;
      }
      const rejectForAbort = (): void => {
        reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      };
      if (signal.aborted) rejectForAbort();
      else signal.addEventListener("abort", rejectForAbort, { once: true });
    });
}

function jsonResponse(
  body: unknown,
  status = 200,
  headers: HeadersInit = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function requestURL(input: string | URL | Request): URL {
  return input instanceof Request ? new URL(input.url) : new URL(input);
}

function providerPayload(id = "openrouter"): Record<string, unknown> {
  return {
    id,
    type: "model_provider",
    display_name: "OpenRouter",
    adapter: "openai",
    credential_type: "api_key",
    default_model: "deepseek/deepseek-v4-pro",
    capabilities: {
      streaming: true,
      tool_calls: true,
      multimodal_input: true,
      reasoning: true,
      native_structured_output: false,
    },
  };
}

function apiKeyPayload(): Record<string, unknown> {
  return {
    id: "key_1",
    type: "api_key",
    organization_id: "org_1",
    name: "Production",
    prefix: "vma_test_key",
    scopes: ["api", "api_keys:manage"],
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
  };
}

async function captureError(value: PromiseLike<unknown>): Promise<unknown> {
  try {
    await value;
    return null;
  } catch (error) {
    return error;
  }
}

function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

function clearConfigurationEnvironment(): void {
  for (const name of configurationEnvironmentNames) delete process.env[name];
}

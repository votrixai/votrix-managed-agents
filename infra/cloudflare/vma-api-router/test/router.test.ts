import { describe, expect, it } from "vitest";

import {
  parseRouterConfiguration,
  routeRequest,
  type RouterBindings,
  type UpstreamFetch,
} from "../src/router.ts";

const STAGING_ORIGIN =
  "https://votrix-managed-agents-staging-eqp3ofm5eq-uc.a.run.app";

function stagingEnv(overrides: Partial<RouterBindings> = {}): RouterBindings {
  return {
    ENVIRONMENT: "staging",
    PUBLIC_HOSTNAME: "staging-api.votrixai.com",
    ORIGIN_URL: STAGING_ORIGIN,
    ...overrides,
  };
}

async function readErrorCode(response: Response): Promise<string> {
  const payload: unknown = await response.json();
  if (
    typeof payload !== "object" ||
    payload === null ||
    !("error" in payload) ||
    typeof payload.error !== "object" ||
    payload.error === null ||
    !("code" in payload.error) ||
    typeof payload.error.code !== "string"
  ) {
    throw new Error("Expected a structured router error");
  }
  return payload.error.code;
}

describe("router configuration", () => {
  it("accepts the exact staging Cloud Run origin", () => {
    const config = parseRouterConfiguration(stagingEnv());
    expect(config.environment).toBe("staging");
    expect(config.publicHostname).toBe("staging-api.votrixai.com");
    expect(config.origin.origin).toBe(STAGING_ORIGIN);
  });

  it.each([
    "http://service.run.app",
    "https://service.run.app.evil.example",
    "https://run.app",
    "https://service.run.app:8443",
    "https://user:password@service.run.app",
    "https://service.run.app/path",
    "https://service.run.app?query=yes",
    "https://service.run.app#fragment",
    "https://REPLACE_WITH_PRODUCTION_CLOUD_RUN_URL.invalid",
  ])("rejects invalid origin %s", (origin) => {
    expect(() =>
      parseRouterConfiguration(stagingEnv({ ORIGIN_URL: origin })),
    ).toThrow();
  });

  it("rejects a public hostname that does not match its environment", () => {
    expect(() =>
      parseRouterConfiguration(
        stagingEnv({ PUBLIC_HOSTNAME: "api.votrixai.com" }),
      ),
    ).toThrow();
  });
});

describe("request proxying", () => {
  it("preserves path, query, authorization, and a streamed request body", async () => {
    let captured: Request | undefined;
    let capturedBody = "";
    const fetchUpstream: UpstreamFetch = async (request) => {
      captured = request;
      capturedBody = await request.text();
      return Response.json({ ok: true });
    };
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('{"streamed":'));
        controller.enqueue(encoder.encode("true}"));
        controller.close();
      },
    });

    const response = await routeRequest(
      new Request("https://staging-api.votrixai.com/v1/sessions?limit=2", {
        method: "POST",
        headers: {
          authorization: "Bearer test-token",
          "content-type": "application/json",
          "request-id": "client-request-123",
          "x-request-id": "must-be-overwritten",
        },
        body,
      }),
      stagingEnv(),
      fetchUpstream,
    );

    expect(response.status).toBe(200);
    expect(captured).toBeDefined();
    expect(captured?.url).toBe(`${STAGING_ORIGIN}/v1/sessions?limit=2`);
    expect(captured?.cache).toBe("no-store");
    expect(captured?.redirect).toBe("manual");
    expect(captured?.headers.get("authorization")).toBe("Bearer test-token");
    expect(captured?.headers.get("x-forwarded-host")).toBe(
      "staging-api.votrixai.com",
    );
    expect(captured?.headers.get("x-forwarded-proto")).toBe("https");
    expect(captured?.headers.get("request-id")).toBe("client-request-123");
    expect(captured?.headers.get("x-request-id")).toBe("client-request-123");
    expect(response.headers.get("request-id")).toBe("client-request-123");
    expect(response.headers.get("x-request-id")).toBe("client-request-123");
    expect(capturedBody).toBe('{"streamed":true}');
  });

  it("streams an SSE response and forces edge and browser no-store", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchUpstream: UpstreamFetch = async () =>
      new Response(body, {
        headers: {
          "cache-control": "public, max-age=3600",
          "content-type": "text/event-stream",
        },
      });

    const response = await routeRequest(
      new Request("https://staging-api.votrixai.com/v1/sessions/sess_123/events"),
      stagingEnv(),
      fetchUpstream,
    );

    expect(response.headers.get("content-type")).toBe("text/event-stream");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("cdn-cache-control")).toBe("no-store");
    expect(response.body).not.toBeNull();

    const reader = response.body?.getReader();
    const readPromise = reader?.read();
    streamController?.enqueue(new TextEncoder().encode("data: ready\n\n"));
    const firstChunk = await readPromise;
    expect(new TextDecoder().decode(firstChunk?.value)).toBe("data: ready\n\n");
    streamController?.close();
    await reader?.cancel();
  });

  it.each([
    [
      `${STAGING_ORIGIN}/v1/sessions/sess_123`,
      "https://staging-api.votrixai.com/v1/sessions/sess_123",
    ],
    ["/health?from=origin", "https://staging-api.votrixai.com/health?from=origin"],
  ])("rewrites same-origin Location %s", async (location, expected) => {
    const response = await routeRequest(
      new Request("https://staging-api.votrixai.com/start"),
      stagingEnv(),
      async () => new Response(null, { status: 307, headers: { location } }),
    );
    expect(response.headers.get("location")).toBe(expected);
  });

  it("does not rewrite a cross-origin Location", async () => {
    const response = await routeRequest(
      new Request("https://staging-api.votrixai.com/start"),
      stagingEnv(),
      async () =>
        new Response(null, {
          status: 302,
          headers: { location: "https://accounts.example.com/login" },
        }),
    );
    expect(response.headers.get("location")).toBe(
      "https://accounts.example.com/login",
    );
  });
});

describe("fail-closed behavior", () => {
  it("rejects an unexpected hostname before fetching upstream", async () => {
    let fetched = false;
    const response = await routeRequest(
      new Request("https://api.votrixai.com/health"),
      stagingEnv(),
      async () => {
        fetched = true;
        return new Response("unexpected");
      },
    );
    expect(response.status).toBe(421);
    expect(await readErrorCode(response)).toBe("misdirected_request");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(fetched).toBe(false);
  });

  it("rejects plaintext HTTP", async () => {
    const response = await routeRequest(
      new Request("http://staging-api.votrixai.com/health"),
      stagingEnv(),
      async () => new Response("unexpected"),
    );
    expect(response.status).toBe(426);
    expect(await readErrorCode(response)).toBe("https_required");
  });

  it("returns a structured 500 for the production placeholder", async () => {
    const response = await routeRequest(
      new Request("https://api.votrixai.com/health"),
      {
        ENVIRONMENT: "production",
        PUBLIC_HOSTNAME: "api.votrixai.com",
        ORIGIN_URL: "https://REPLACE_WITH_PRODUCTION_CLOUD_RUN_URL.invalid",
      },
      async () => new Response("unexpected"),
    );
    expect(response.status).toBe(500);
    expect(await readErrorCode(response)).toBe("router_misconfigured");
  });

  it("returns a structured 502 without exposing the upstream error", async () => {
    const response = await routeRequest(
      new Request("https://staging-api.votrixai.com/health"),
      stagingEnv(),
      async () => {
        throw new Error("private transport detail");
      },
    );
    expect(response.status).toBe(502);
    expect(await readErrorCode(response)).toBe("upstream_unavailable");
  });
});

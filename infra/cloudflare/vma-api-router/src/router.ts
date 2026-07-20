const EXPECTED_PUBLIC_HOSTNAMES = {
  staging: "staging-api.vma.votrixai.com",
  production: "api.vma.votrixai.com",
} as const;

const HOP_BY_HOP_HEADERS = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
] as const;

type EnvironmentName = keyof typeof EXPECTED_PUBLIC_HOSTNAMES;

/**
 * Binding names come from Wrangler's generated Env interface. The mapped type
 * only widens environment-specific literal values so one source can validate
 * both named Wrangler environments and tests can exercise invalid config.
 */
export type RouterBindings = {
  [Key in keyof Pick<
    Env,
    "ENVIRONMENT" | "PUBLIC_HOSTNAME" | "ORIGIN_URL"
  >]: string;
};

export type UpstreamFetch = (request: Request) => Promise<Response>;

export interface RouterConfiguration {
  environment: EnvironmentName;
  publicHostname: string;
  origin: URL;
}

class RouterFailure extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    readonly publicMessage: string,
    internalMessage: string,
  ) {
    super(internalMessage);
    this.name = "RouterFailure";
  }
}

function isEnvironmentName(value: string | undefined): value is EnvironmentName {
  return value === "staging" || value === "production";
}

function isDnsHostname(hostname: string): boolean {
  if (hostname.length > 253) return false;
  return hostname.split(".").every((label) => {
    return (
      label.length > 0 &&
      label.length <= 63 &&
      /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label)
    );
  });
}

export function parseRouterConfiguration(
  env: RouterBindings,
): RouterConfiguration {
  if (!isEnvironmentName(env.ENVIRONMENT)) {
    throw new RouterFailure(
      "router_misconfigured",
      500,
      "API router is not configured.",
      `Unsupported ENVIRONMENT: ${env.ENVIRONMENT}`,
    );
  }

  const expectedPublicHostname = EXPECTED_PUBLIC_HOSTNAMES[env.ENVIRONMENT];
  if (env.PUBLIC_HOSTNAME !== expectedPublicHostname) {
    throw new RouterFailure(
      "router_misconfigured",
      500,
      "API router is not configured.",
      `PUBLIC_HOSTNAME must be ${expectedPublicHostname}`,
    );
  }

  if (typeof env.ORIGIN_URL !== "string") {
    throw new RouterFailure(
      "router_misconfigured",
      500,
      "API router is not configured.",
      "ORIGIN_URL is missing",
    );
  }

  let origin: URL;
  try {
    origin = new URL(env.ORIGIN_URL);
  } catch {
    throw new RouterFailure(
      "router_misconfigured",
      500,
      "API router is not configured.",
      "ORIGIN_URL is not a valid URL",
    );
  }

  const isBareOrigin =
    origin.pathname === "/" &&
    origin.search === "" &&
    origin.hash === "" &&
    origin.username === "" &&
    origin.password === "" &&
    origin.port === "";
  const isCloudRunHostname =
    origin.hostname.endsWith(".run.app") &&
    origin.hostname !== "run.app" &&
    isDnsHostname(origin.hostname);

  if (origin.protocol !== "https:" || !isBareOrigin || !isCloudRunHostname) {
    throw new RouterFailure(
      "router_misconfigured",
      500,
      "API router is not configured.",
      "ORIGIN_URL must be a bare https://*.run.app origin without credentials, port, path, query, or fragment",
    );
  }

  return {
    environment: env.ENVIRONMENT,
    publicHostname: expectedPublicHostname,
    origin,
  };
}

function isPublicEdgePath(pathname: string): boolean {
  return (
    pathname === "/" ||
    pathname === "/openapi.json" ||
    pathname === "/health" ||
    pathname.startsWith("/health/") ||
    pathname === "/v1" ||
    pathname.startsWith("/v1/")
  );
}

function isValidRequestId(value: string | null): value is string {
  return value !== null && /^[a-zA-Z0-9._:-]{1,128}$/.test(value);
}

function getRequestId(request: Request): string {
  for (const header of ["request-id", "x-request-id", "cf-ray"] as const) {
    const candidate = request.headers.get(header);
    if (isValidRequestId(candidate)) return candidate;
  }
  return crypto.randomUUID();
}

function errorResponse(failure: RouterFailure, requestId: string): Response {
  return Response.json(
    {
      error: {
        code: failure.code,
        message: failure.publicMessage,
        request_id: requestId,
      },
    },
    {
      status: failure.status,
      headers: {
        "cache-control": "no-store",
        "cdn-cache-control": "no-store",
        "request-id": requestId,
        "x-request-id": requestId,
      },
    },
  );
}

function buildUpstreamRequest(
  request: Request,
  incomingUrl: URL,
  config: RouterConfiguration,
  requestId: string,
): Request {
  const target = new URL(incomingUrl.pathname + incomingUrl.search, config.origin);
  const headers = new Headers(request.headers);

  headers.delete("host");
  for (const header of HOP_BY_HOP_HEADERS) headers.delete(header);
  headers.set("x-forwarded-host", config.publicHostname);
  headers.set("x-forwarded-proto", "https");
  headers.set("request-id", requestId);
  headers.set("x-request-id", requestId);

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
    cache: "no-store",
    signal: request.signal,
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
  }

  return new Request(target, init);
}

function buildPublicResponse(
  upstream: Response,
  config: RouterConfiguration,
  requestId: string,
): Response {
  const headers = new Headers(upstream.headers);
  for (const header of HOP_BY_HOP_HEADERS) headers.delete(header);

  headers.set("cache-control", "no-store");
  headers.set("cdn-cache-control", "no-store");
  headers.set("request-id", requestId);
  headers.set("x-request-id", requestId);

  const location = headers.get("location");
  if (location !== null && upstream.status >= 300 && upstream.status < 400) {
    let redirectTarget: URL | undefined;
    try {
      redirectTarget = new URL(location, config.origin);
    } catch {
      redirectTarget = undefined;
    }

    if (redirectTarget?.origin === config.origin.origin) {
      redirectTarget.protocol = "https:";
      redirectTarget.username = "";
      redirectTarget.password = "";
      redirectTarget.host = config.publicHostname;
      headers.set("location", redirectTarget.toString());
    }
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}

function logEvent(
  level: "info" | "warn" | "error",
  event: Record<string, string | number>,
): void {
  const encoded = JSON.stringify(event);
  if (level === "error") {
    console.error(encoded);
  } else if (level === "warn") {
    console.warn(encoded);
  } else {
    console.log(encoded);
  }
}

export async function routeRequest(
  request: Request,
  env: RouterBindings,
  fetchUpstream: UpstreamFetch = (upstreamRequest) => fetch(upstreamRequest),
): Promise<Response> {
  const requestId = getRequestId(request);
  const incomingUrl = new URL(request.url);
  const startedAt = Date.now();

  try {
    const config = parseRouterConfiguration(env);

    if (incomingUrl.protocol !== "https:") {
      throw new RouterFailure(
        "https_required",
        426,
        "HTTPS is required.",
        `Rejected protocol: ${incomingUrl.protocol}`,
      );
    }
    if (
      incomingUrl.hostname !== config.publicHostname ||
      incomingUrl.port !== ""
    ) {
      throw new RouterFailure(
        "misdirected_request",
        421,
        "Request hostname is not served by this router.",
        `Rejected hostname: ${incomingUrl.host}`,
      );
    }
    if (!isPublicEdgePath(incomingUrl.pathname)) {
      throw new RouterFailure(
        "not_found",
        404,
        "Not found.",
        `Blocked non-public path at the public edge: ${incomingUrl.pathname}`,
      );
    }

    const upstreamRequest = buildUpstreamRequest(
      request,
      incomingUrl,
      config,
      requestId,
    );
    let upstream: Response;
    try {
      upstream = await fetchUpstream(upstreamRequest);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new RouterFailure(
        "upstream_unavailable",
        502,
        "Upstream service is unavailable.",
        detail,
      );
    }

    const response = buildPublicResponse(upstream, config, requestId);
    logEvent("info", {
      event: "api_router_request",
      environment: config.environment,
      method: request.method,
      path: incomingUrl.pathname,
      status: response.status,
      duration_ms: Date.now() - startedAt,
      request_id: requestId,
    });
    return response;
  } catch (error) {
    const failure =
      error instanceof RouterFailure
        ? error
        : new RouterFailure(
            "internal_error",
            500,
            "Internal router error.",
            error instanceof Error ? error.message : String(error),
          );
    const level = failure.status >= 500 ? "error" : "warn";
    logEvent(level, {
      event: "api_router_error",
      code: failure.code,
      method: request.method,
      path: incomingUrl.pathname,
      status: failure.status,
      duration_ms: Date.now() - startedAt,
      request_id: requestId,
      detail: failure.message,
    });
    return errorResponse(failure, requestId);
  }
}

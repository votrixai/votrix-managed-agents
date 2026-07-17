import { randomUUID } from "node:crypto";

import { APIPromise, type APIResponse } from "./api-promise.js";
import {
  APIConnectionError,
  APIResponseValidationError,
  APIStatusError,
  APITimeoutError,
  VotrixError,
  statusErrorClass,
} from "./errors.js";
import { PagePromise, type CursorKind } from "./pagination.js";
import { EventStream } from "./streaming.js";
import { DEFAULT_BETA, VERSION } from "./version.js";

export type AuthScheme = "x-api-key" | "bearer";
export type Fetch = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;
export type QueryPrimitive = string | number | boolean;
export type QueryValue =
  QueryPrimitive | readonly QueryPrimitive[] | null | undefined;
export type Query = Readonly<Record<string, QueryValue>>;
export type Sanitizer<T> = (value: unknown) => T;

export interface RequestOptions {
  headers?: HeadersInit;
  signal?: AbortSignal;
  timeout?: number;
  maxRetries?: number;
  idempotencyKey?: string;
  retry?: boolean;
}

export interface VotrixOptions {
  apiKey?: string;
  baseURL?: string;
  authScheme?: AuthScheme;
  beta?: string;
  timeout?: number;
  maxRetries?: number;
  defaultHeaders?: HeadersInit;
  fetch?: Fetch;
}

export { APIPromise } from "./api-promise.js";
export type {
  APIResponse,
  WithRequestID,
  WithResponse,
} from "./api-promise.js";
export { PagePromise } from "./pagination.js";

export interface InternalRequest<T> {
  query?: Query | undefined;
  body?: unknown;
  form?: FormData | undefined;
  headers?: HeadersInit | undefined;
  options?: RequestOptions | undefined;
  sanitize?: Sanitizer<T> | undefined;
}

export interface PageRequest<T> {
  query?: Query | undefined;
  cursor: CursorKind;
  options?: RequestOptions | undefined;
  sanitize?: Sanitizer<T> | undefined;
}

export interface StreamRequest {
  query?: Query | undefined;
  headers?: HeadersInit | undefined;
  maxReconnects?: number | undefined;
  options?: RequestOptions | undefined;
}

export interface BinaryRequest {
  query?: Query | undefined;
  options?: RequestOptions | undefined;
}

interface RetryState {
  initialized: boolean;
  remaining: number;
  attempt: number;
}

/** Shared transport used by all public resources. */
export class APIClient {
  readonly apiKey: string;
  readonly baseURL: string;
  readonly authScheme: AuthScheme;
  readonly timeout: number;
  readonly maxRetries: number;

  private readonly fetcher: Fetch;
  private readonly defaultHeaders: Headers;
  private closed = false;

  constructor(options: VotrixOptions = {}) {
    if (isBrowser()) {
      throw new VotrixErrorForConfiguration(
        "Votrix is a server-side SDK and cannot expose an Organization API key in a browser.",
      );
    }

    const apiKey = options.apiKey ?? readEnvironment("VOTRIX_API_KEY");
    if (!apiKey) {
      throw new VotrixErrorForConfiguration(
        "apiKey is required; pass it explicitly or set VOTRIX_API_KEY",
      );
    }
    const baseURL = (
      options.baseURL ??
      readEnvironment("VOTRIX_BASE_URL") ??
      ""
    ).trim();
    if (!baseURL) {
      throw new VotrixErrorForConfiguration(
        "baseURL is required; pass it explicitly or set VOTRIX_BASE_URL",
      );
    }

    const authScheme = options.authScheme ?? "x-api-key";
    if (authScheme !== "x-api-key" && authScheme !== "bearer") {
      throw new VotrixErrorForConfiguration(
        "authScheme must be 'x-api-key' or 'bearer'",
      );
    }
    const timeout = options.timeout ?? 60_000;
    const maxRetries = options.maxRetries ?? 2;
    validateNonNegativeFinite("timeout", timeout);
    validateNonNegativeInteger("maxRetries", maxRetries);

    const fetcher = options.fetch ?? globalThis.fetch;
    if (typeof fetcher !== "function") {
      throw new VotrixErrorForConfiguration(
        "A Fetch implementation is required. Votrix supports Node.js 22 and newer.",
      );
    }

    this.apiKey = apiKey;
    this.baseURL = `${baseURL.replace(/\/+$/, "")}/`;
    this.authScheme = authScheme;
    this.timeout = timeout;
    this.maxRetries = maxRetries;
    this.fetcher = fetcher.bind(globalThis);
    this.defaultHeaders = new Headers(options.defaultHeaders);
    setHeaderIfMissing(this.defaultHeaders, "accept", "application/json");
    setHeaderIfMissing(
      this.defaultHeaders,
      "user-agent",
      `votrix-typescript/${VERSION}`,
    );
    setHeaderIfMissing(this.defaultHeaders, "x-votrix-sdk-version", VERSION);
    setHeaderIfMissing(
      this.defaultHeaders,
      "votrix-managed-agents-beta",
      options.beta ?? DEFAULT_BETA,
    );
  }

  close(): void {
    this.closed = true;
  }

  request<T>(
    method: string,
    path: string,
    request: InternalRequest<T> = {},
  ): APIPromise<T> {
    const retryState = createRetryState();
    const response = this.execute(method, path, request, retryState);
    return new APIPromise(response, async (rawResponse) =>
      this.parseResponse(rawResponse, method, path, request, retryState),
    );
  }

  getPage<T>(path: string, request: PageRequest<T>): PagePromise<T> {
    return new PagePromise<T>(this, path, request);
  }

  binary(
    path: string,
    request: BinaryRequest = {},
  ): APIPromise<BinaryResponse> {
    const responsePromise = this.execute("GET", path, {
      query: request.query,
      headers: { accept: "application/octet-stream" },
      options: request.options,
    });
    return new APIPromise(responsePromise, async (response) => ({
      data: new BinaryResponse(
        response,
        request.options?.signal,
        request.options?.timeout ?? this.timeout,
      ),
      response,
      requestID: requestID(response),
    }));
  }

  async stream(
    path: string,
    request: StreamRequest = {},
  ): Promise<EventStream> {
    const maxReconnects = request.maxReconnects ?? this.maxRetries;
    validateNonNegativeInteger("maxReconnects", maxReconnects);
    return new EventStream(this, {
      path,
      query: request.query,
      headers: request.headers,
      maxReconnects,
      options: request.options,
    });
  }

  async openStream(
    path: string,
    request: {
      query?: Query | undefined;
      headers?: HeadersInit | undefined;
      options?: RequestOptions | undefined;
    },
  ): Promise<Response> {
    return await this.execute("GET", path, {
      query: request.query,
      headers: mergeHeaders({ accept: "text/event-stream" }, request.headers),
      options: request.options,
    });
  }

  idempotencyKey(options?: RequestOptions): string {
    if (options?.idempotencyKey) return options.idempotencyKey;
    const headerKey = new Headers(options?.headers).get("idempotency-key");
    return headerKey || randomUUID();
  }

  sanitizeApiKey<T>(value: unknown, allowSecret = false): T {
    const source = requireObject(value, "API key");
    requireStringFields(source, [
      "id",
      "type",
      "organization_id",
      "name",
      "prefix",
      "created_at",
      "updated_at",
    ]);
    if (!Array.isArray(source.scopes)) {
      throw new TypeError("Expected API key response scopes to be an array");
    }
    const safe = pick(source, [
      "id",
      "type",
      "organization_id",
      "name",
      "prefix",
      "scopes",
      "expires_at",
      "created_by",
      "metadata",
      "last_used_at",
      "revoked_at",
      "revoked_by",
      "revocation_reason",
      "replaced_by_key_id",
      "replaces_key_id",
      "created_at",
      "updated_at",
    ]);
    if (allowSecret) {
      if (typeof source.secret !== "string" || !source.secret) {
        throw new TypeError(
          "Expected API key create/rotate response to contain a secret",
        );
      }
      safe.secret = source.secret;
    }
    return safe as T;
  }

  sanitizeModelCredential<T>(value: unknown): T {
    const source = requireObject(value, "model Credential");
    requireStringFields(source, ["id", "type", "vault_id", "model_provider"]);
    return pick(source, [
      "id",
      "type",
      "vault_id",
      "model_provider",
      "display_name",
      "metadata",
      "archived_at",
      "created_at",
      "updated_at",
    ]) as T;
  }

  private async parseResponse<T>(
    initialResponse: Response,
    method: string,
    path: string,
    request: InternalRequest<T>,
    retryState: RetryState,
  ): Promise<APIResponse<T>> {
    let response = initialResponse;
    while (true) {
      let text: string;
      try {
        text = await readResponseText(
          response,
          request.options?.signal,
          request.options?.timeout ?? this.timeout,
        );
      } catch (error) {
        if (
          (error instanceof APIConnectionError ||
            error instanceof APITimeoutError) &&
          !request.options?.signal?.aborted
        ) {
          const retryAttempt = takeRetry(retryState);
          if (retryAttempt !== null) {
            await sleep(retryDelay(retryAttempt), request.options?.signal);
            response = await this.execute(method, path, request, retryState);
            continue;
          }
        }
        throw error;
      }

      let value: unknown;
      try {
        value = text ? (JSON.parse(text) as unknown) : undefined;
      } catch (error) {
        throw new APIResponseValidationError(
          "Invalid JSON response from Votrix",
          {
            statusCode: response.status,
            requestID: requestID(response),
            headers: response.headers,
            cause: error,
          },
        );
      }

      try {
        const data = request.sanitize ? request.sanitize(value) : (value as T);
        attachRequestID(data, requestID(response));
        return { data, response, requestID: requestID(response) };
      } catch (error) {
        if (error instanceof APIResponseValidationError) throw error;
        throw new APIResponseValidationError(
          "Invalid response shape from Votrix",
          {
            statusCode: response.status,
            requestID: requestID(response),
            headers: response.headers,
            cause: error,
          },
        );
      }
    }
  }

  private async execute(
    method: string,
    path: string,
    request: Omit<InternalRequest<unknown>, "sanitize">,
    retryState: RetryState = createRetryState(),
  ): Promise<Response> {
    this.ensureOpen();
    const normalizedMethod = method.toUpperCase();
    const headers = this.buildHeaders(request.headers, request.options);
    const url = buildURL(this.baseURL, path, request.query);
    const body = encodeBody(request.body, request.form, headers);
    const requestSecrets = collectSecrets(request.body, this.apiKey);
    const isReplaySafe =
      SAFE_METHODS.has(normalizedMethod) || headers.has("idempotency-key");
    if (!retryState.initialized) {
      const configuredRetries = request.options?.maxRetries ?? this.maxRetries;
      validateNonNegativeInteger("maxRetries", configuredRetries);
      retryState.remaining =
        isReplaySafe && request.options?.retry !== false
          ? configuredRetries
          : 0;
      retryState.initialized = true;
    }

    while (true) {
      const timeout = request.options?.timeout ?? this.timeout;
      validateNonNegativeFinite("timeout", timeout);
      const abort = createAbortContext(request.options?.signal, timeout);
      try {
        const response = await this.fetcher(url, {
          method: normalizedMethod,
          headers,
          body,
          signal: abort.signal,
        });
        abort.cleanup();

        if (RETRYABLE_STATUSES.has(response.status)) {
          const retryAttempt = takeRetry(retryState);
          if (retryAttempt !== null) {
            await cancelResponse(response);
            await sleep(
              retryDelay(retryAttempt, response),
              request.options?.signal,
            );
            continue;
          }
        }
        if (!response.ok) {
          await this.raiseStatus(
            response,
            requestSecrets,
            request.options?.signal,
            timeout,
          );
        }
        return response;
      } catch (error) {
        abort.cleanup();
        if (
          error instanceof APIStatusError ||
          error instanceof APIResponseValidationError
        ) {
          throw error;
        }
        if (request.options?.signal?.aborted) {
          throw new APIConnectionError("Request to Votrix was aborted", {
            cause: error,
          });
        }
        const timedOut = abort.timedOut();
        const retryAttempt = takeRetry(retryState);
        if (retryAttempt !== null) {
          await sleep(retryDelay(retryAttempt), request.options?.signal);
          continue;
        }
        if (timedOut) {
          throw new APITimeoutError("Request to Votrix timed out", {
            cause: error,
          });
        }
        throw new APIConnectionError("Could not connect to Votrix", {
          cause: error,
        });
      }
    }
    throw new APIConnectionError("Could not connect to Votrix");
  }

  private buildHeaders(
    headers?: HeadersInit,
    options?: RequestOptions,
  ): Headers {
    const merged = new Headers(this.defaultHeaders);
    copyHeaders(merged, headers);
    copyHeaders(merged, options?.headers);
    if (this.authScheme === "bearer") {
      merged.set("authorization", `Bearer ${this.apiKey}`);
      merged.delete("x-api-key");
    } else {
      merged.set("x-api-key", this.apiKey);
      merged.delete("authorization");
    }
    if (options?.idempotencyKey)
      merged.set("idempotency-key", options.idempotencyKey);
    return merged;
  }

  private async raiseStatus(
    response: Response,
    secrets: ReadonlySet<string>,
    signal: AbortSignal | undefined,
    timeout: number,
  ): Promise<never> {
    let rawBody: unknown;
    try {
      const text = await readResponseText(response, signal, timeout);
      rawBody = text ? (JSON.parse(text) as unknown) : undefined;
    } catch {
      rawBody = undefined;
    }
    const body = redact(rawBody, secrets);
    const errorObject =
      isRecord(body) && isRecord(body.error) ? body.error : undefined;
    const errorType = stringOrNull(errorObject?.type);
    const errorCode = stringOrNull(errorObject?.code);
    const detail = isRecord(body) ? body.detail : undefined;
    let message = stringOrNull(errorObject?.message) ?? stringOrNull(detail);
    if (!message)
      message = `Votrix API request failed with status ${response.status}`;
    for (const secret of secrets) {
      if (secret) message = message.split(secret).join("[redacted]");
    }
    const ErrorClass = statusErrorClass(response.status);
    throw new ErrorClass(message, {
      statusCode: response.status,
      errorType,
      errorCode,
      requestID: requestID(response),
      body,
      response,
    });
  }

  private ensureOpen(): void {
    if (this.closed)
      throw new VotrixErrorForConfiguration("Votrix client is closed");
  }
}

/** A Fetch response wrapper that keeps binary downloads streaming by default. */
export class BinaryResponse {
  readonly response: Response;
  readonly statusCode: number;
  readonly headers: Headers;
  readonly contentType: string | null;
  readonly filename: string | null;
  private readonly controller = new AbortController();
  private readonly parentSignal: AbortSignal | undefined;
  private readonly parentAbortListener: (() => void) | undefined;
  private readonly readTimeout: number;
  private explicitlyClosed = false;

  constructor(response: Response, signal?: AbortSignal, readTimeout = 0) {
    this.response = response;
    this.statusCode = response.status;
    this.headers = response.headers;
    this.contentType = response.headers.get("content-type");
    this.filename = contentDispositionFilename(
      response.headers.get("content-disposition"),
    );
    validateNonNegativeFinite("timeout", readTimeout);
    this.readTimeout = readTimeout;
    this.parentSignal = signal;
    if (signal) {
      this.parentAbortListener = () =>
        this.controller.abort(this.parentSignal?.reason);
      if (signal.aborted) this.controller.abort(signal.reason);
      else
        signal.addEventListener("abort", this.parentAbortListener, {
          once: true,
        });
    } else {
      this.parentAbortListener = undefined;
    }
  }

  get body(): ReadableStream<Uint8Array> | null {
    return this.response.body;
  }

  async arrayBuffer(): Promise<ArrayBuffer> {
    const bytes = await this.bytes();
    const buffer = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(buffer).set(bytes);
    return buffer;
  }

  async bytes(): Promise<Uint8Array> {
    const chunks: Uint8Array[] = [];
    let length = 0;
    for await (const chunk of this.iterBytes()) {
      chunks.push(chunk);
      length += chunk.byteLength;
    }
    const result = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return result;
  }

  async read(): Promise<Uint8Array> {
    return await this.bytes();
  }

  async text(): Promise<string> {
    const decoder = new TextDecoder();
    let value = "";
    for await (const chunk of this.iterBytes())
      value += decoder.decode(chunk, { stream: true });
    return value + decoder.decode();
  }

  async *iterBytes(): AsyncGenerator<Uint8Array> {
    if (!this.response.body) {
      this.detachParentSignal();
      return;
    }
    const reader = this.response.body.getReader();
    let finished = false;
    let readTimedOut = false;
    const onAbort = (): void => {
      void reader.cancel(this.controller.signal.reason);
    };
    if (this.controller.signal.aborted) onAbort();
    else
      this.controller.signal.addEventListener("abort", onAbort, { once: true });
    try {
      while (true) {
        readTimedOut = false;
        const timer =
          this.readTimeout === 0
            ? undefined
            : setTimeout(() => {
                readTimedOut = true;
                void reader.cancel(new Error("Binary response read timed out"));
              }, this.readTimeout);
        timer?.unref?.();
        let result: ReadableStreamReadResult<Uint8Array>;
        try {
          result = await reader.read();
        } finally {
          if (timer !== undefined) clearTimeout(timer);
        }
        if (readTimedOut) {
          throw new APITimeoutError(
            "Request to Votrix timed out while reading a binary response",
          );
        }
        if (this.controller.signal.aborted) {
          if (this.explicitlyClosed) return;
          throw new APIConnectionError(
            "Request to Votrix was aborted while reading a binary response",
          );
        }
        if (result.done) {
          finished = true;
          return;
        }
        yield result.value;
      }
    } catch (error) {
      if (
        error instanceof APITimeoutError ||
        error instanceof APIConnectionError
      ) {
        throw error;
      }
      if (readTimedOut) {
        throw new APITimeoutError(
          "Request to Votrix timed out while reading a binary response",
          { cause: error },
        );
      }
      if (this.controller.signal.aborted) {
        if (this.explicitlyClosed) return;
        throw new APIConnectionError(
          "Request to Votrix was aborted while reading a binary response",
          { cause: error },
        );
      }
      throw new APIConnectionError(
        "Connection to Votrix closed while reading a binary response",
        { cause: error },
      );
    } finally {
      this.controller.signal.removeEventListener("abort", onAbort);
      this.detachParentSignal();
      if (!finished) {
        try {
          await reader.cancel();
        } catch {
          // The response has already closed.
        }
      }
      reader.releaseLock();
    }
  }

  async writeToFile(path: string): Promise<string> {
    const { open } = await import("node:fs/promises");
    const file = await open(path, "w");
    try {
      for await (const chunk of this.iterBytes()) {
        let offset = 0;
        while (offset < chunk.byteLength) {
          const { bytesWritten } = await file.write(chunk, offset);
          offset += bytesWritten;
        }
      }
    } finally {
      await file.close();
    }
    return path;
  }

  async close(): Promise<void> {
    this.explicitlyClosed = true;
    this.detachParentSignal();
    this.controller.abort(new Error("Binary response closed"));
    try {
      await this.response.body?.cancel();
    } catch {
      // A consumed or already-closed body is already released.
    }
  }

  private detachParentSignal(): void {
    if (this.parentAbortListener) {
      this.parentSignal?.removeEventListener("abort", this.parentAbortListener);
    }
  }
}

class VotrixErrorForConfiguration extends VotrixError {}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const RETRYABLE_STATUSES = new Set([408, 429, 500, 502, 503, 504, 529]);

function createRetryState(): RetryState {
  return { initialized: false, remaining: 0, attempt: 0 };
}

function takeRetry(state: RetryState): number | null {
  if (state.remaining <= 0) return null;
  state.remaining -= 1;
  const attempt = state.attempt;
  state.attempt += 1;
  return attempt;
}

function readEnvironment(name: string): string | undefined {
  return typeof process !== "undefined" ? process.env[name] : undefined;
}

function isBrowser(): boolean {
  return (
    typeof window !== "undefined" && typeof window.document !== "undefined"
  );
}

function validateNonNegativeFinite(name: string, value: number): void {
  if (!Number.isFinite(value) || value < 0) {
    throw new VotrixErrorForConfiguration(
      `${name} must be a non-negative finite number`,
    );
  }
}

function validateNonNegativeInteger(name: string, value: number): void {
  if (!Number.isInteger(value) || value < 0) {
    throw new VotrixErrorForConfiguration(
      `${name} must be a non-negative integer`,
    );
  }
}

function setHeaderIfMissing(
  headers: Headers,
  name: string,
  value: string,
): void {
  if (!headers.has(name)) headers.set(name, value);
}

function copyHeaders(target: Headers, source?: HeadersInit): void {
  if (!source) return;
  new Headers(source).forEach((value, name) => target.set(name, value));
}

function mergeHeaders(...sources: Array<HeadersInit | undefined>): Headers {
  const result = new Headers();
  for (const source of sources) copyHeaders(result, source);
  return result;
}

function buildURL(baseURL: string, path: string, query?: Query): URL {
  const url = new URL(path.replace(/^\/+/, ""), baseURL);
  if (!query) return url;
  for (const [name, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) url.searchParams.append(name, String(item));
    } else {
      url.searchParams.append(name, String(value));
    }
  }
  return url;
}

function encodeBody(
  body: unknown,
  form: FormData | undefined,
  headers: Headers,
): BodyInit | null {
  if (body !== undefined && form !== undefined) {
    throw new VotrixErrorForConfiguration(
      "A request cannot contain both JSON and multipart bodies",
    );
  }
  if (form !== undefined) {
    headers.delete("content-type");
    return form;
  }
  if (body === undefined) return null;
  if (!headers.has("content-type"))
    headers.set("content-type", "application/json");
  return JSON.stringify(body);
}

function createAbortContext(
  parent: AbortSignal | undefined,
  timeout: number,
): {
  signal: AbortSignal;
  timedOut: () => boolean;
  cleanup: () => void;
} {
  const controller = new AbortController();
  let timeoutTriggered = false;
  const onAbort = (): void => controller.abort(parent?.reason);
  if (parent?.aborted) onAbort();
  else parent?.addEventListener("abort", onAbort, { once: true });

  const timer =
    timeout === 0
      ? undefined
      : setTimeout(() => {
          timeoutTriggered = true;
          controller.abort(new Error("Request timed out"));
        }, timeout);
  timer?.unref?.();

  return {
    signal: controller.signal,
    timedOut: () => timeoutTriggered,
    cleanup: () => {
      if (timer !== undefined) clearTimeout(timer);
      parent?.removeEventListener("abort", onAbort);
    },
  };
}

async function readResponseText(
  response: Response,
  parent: AbortSignal | undefined,
  timeout: number,
): Promise<string> {
  validateNonNegativeFinite("timeout", timeout);
  if (!response.body) return "";

  const reader = response.body.getReader();
  const abort = createAbortContext(parent, timeout);
  const decoder = new TextDecoder();
  let text = "";
  let finished = false;
  const cancelForAbort = (): void => {
    void reader.cancel(abort.signal.reason);
  };
  if (abort.signal.aborted) cancelForAbort();
  else abort.signal.addEventListener("abort", cancelForAbort, { once: true });

  try {
    while (true) {
      const result = await reader.read();
      if (result.done) {
        finished = true;
        break;
      }
      text += decoder.decode(result.value, { stream: true });
    }
    text += decoder.decode();
  } catch (error) {
    if (abort.timedOut()) {
      throw new APITimeoutError(
        "Request to Votrix timed out while reading the response",
        { cause: error },
      );
    }
    if (parent?.aborted) {
      throw new APIConnectionError(
        "Request to Votrix was aborted while reading the response",
        { cause: error },
      );
    }
    throw new APIConnectionError(
      "Connection to Votrix closed while reading the response",
      { cause: error },
    );
  } finally {
    abort.signal.removeEventListener("abort", cancelForAbort);
    abort.cleanup();
    if (!finished) {
      try {
        await reader.cancel();
      } catch {
        // The response body has already failed or closed.
      }
    }
    reader.releaseLock();
  }

  if (abort.timedOut()) {
    throw new APITimeoutError(
      "Request to Votrix timed out while reading the response",
    );
  }
  if (parent?.aborted) {
    throw new APIConnectionError(
      "Request to Votrix was aborted while reading the response",
    );
  }
  return text;
}

async function cancelResponse(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // The connection is already closed.
  }
}

async function sleep(
  milliseconds: number,
  signal?: AbortSignal,
): Promise<void> {
  if (milliseconds <= 0) return;
  await new Promise<void>((resolve, reject) => {
    const finish = (): void => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    };
    const timer = setTimeout(finish, milliseconds);
    timer.unref?.();
    const onAbort = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      reject(new APIConnectionError("Request to Votrix was aborted"));
    };
    if (signal?.aborted) onAbort();
    else signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function retryDelay(attempt: number, response?: Response): number {
  const retryAfter = response?.headers.get("retry-after");
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds))
      return Math.min(60_000, Math.max(0, seconds * 1000));
    const retryAt = Date.parse(retryAfter);
    if (!Number.isNaN(retryAt))
      return Math.min(60_000, Math.max(0, retryAt - Date.now()));
  }
  const base = Math.min(8_000, 500 * 2 ** attempt);
  return base * (0.75 + Math.random() * 0.5);
}

function requestID(response: Response): string | null {
  return (
    response.headers.get("request-id") ?? response.headers.get("x-request-id")
  );
}

function attachRequestID(value: unknown, id: string | null): void {
  if (id && isRecord(value) && !("_request_id" in value)) {
    Object.defineProperty(value, "_request_id", {
      value: id,
      enumerable: false,
      configurable: true,
    });
  }
}

function collectSecrets(value: unknown, apiKey: string): Set<string> {
  const secrets = new Set<string>(apiKey ? [apiKey] : []);
  const visit = (current: unknown, key = ""): void => {
    const normalized = key.toLowerCase().replaceAll("-", "_");
    if (typeof current === "string" && isSecretKey(normalized)) {
      if (current) secrets.add(current);
      return;
    }
    if (Array.isArray(current)) {
      for (const child of current) visit(child, key);
    } else if (isRecord(current)) {
      for (const [childKey, child] of Object.entries(current))
        visit(child, childKey);
    }
  };
  visit(value);
  return secrets;
}

function redact(
  value: unknown,
  secrets: ReadonlySet<string>,
  key = "",
): unknown {
  const normalized = key.toLowerCase().replaceAll("-", "_");
  if (Array.isArray(value))
    return value.map((item) => redact(item, secrets, key));
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, child]) => [
        childKey,
        redact(child, secrets, childKey),
      ]),
    );
  }
  if (typeof value !== "string") return value;
  if (isSecretKey(normalized)) return "[redacted]";
  let result = value;
  for (const secret of secrets)
    if (secret) result = result.split(secret).join("[redacted]");
  return result;
}

function isSecretKey(key: string): boolean {
  return ["secret", "token", "api_key", "password"].some((part) =>
    key.includes(part),
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireObject(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value))
    throw new TypeError(`Expected ${label} response to be an object`);
  return value;
}

function requireStringFields(
  value: Record<string, unknown>,
  fields: readonly string[],
): void {
  for (const field of fields) {
    if (typeof value[field] !== "string" || !value[field]) {
      throw new TypeError(
        `Expected response field ${field} to be a non-empty string`,
      );
    }
  }
}

function pick(
  source: Record<string, unknown>,
  keys: readonly string[],
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const key of keys) if (key in source) result[key] = source[key];
  return result;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function contentDispositionFilename(value: string | null): string | null {
  if (!value) return null;
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(value)?.[1];
  if (extended) {
    try {
      return decodeURIComponent(extended.trim().replace(/^"|"$/g, ""));
    } catch {
      return extended.trim().replace(/^"|"$/g, "");
    }
  }
  return (
    /filename\s*=\s*"([^"]+)"/i.exec(value)?.[1] ??
    /filename\s*=\s*([^;]+)/i.exec(value)?.[1]?.trim() ??
    null
  );
}

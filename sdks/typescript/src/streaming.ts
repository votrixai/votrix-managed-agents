import {
  APIConnectionError,
  APIStatusError,
  APIStreamError,
} from "./errors.js";
import type { APIClient, Query, RequestOptions } from "./core.js";
import type { SSEEvent } from "./types.js";

interface EventStreamOptions {
  path: string;
  query?: Query | undefined;
  headers?: HeadersInit | undefined;
  maxReconnects: number;
  options?: RequestOptions | undefined;
}

/** Reconnecting, deduplicating server-sent event stream. */
export class EventStream implements AsyncIterable<SSEEvent> {
  private readonly client: APIClient;
  private readonly path: string;
  private readonly query: Query | undefined;
  private readonly headers: Headers;
  private readonly maxReconnects: number;
  private readonly requestOptions: RequestOptions | undefined;
  private readonly controller = new AbortController();
  private readonly parentSignal: AbortSignal | undefined;
  private readonly parentAbortListener: (() => void) | undefined;
  private response: Response | null = null;
  private started = false;
  private closed = false;

  constructor(client: APIClient, options: EventStreamOptions) {
    this.client = client;
    this.path = options.path;
    this.query = options.query;
    this.headers = new Headers(options.headers);
    this.maxReconnects = options.maxReconnects;
    this.requestOptions = options.options;
    this.parentSignal = options.options?.signal;
    if (this.parentSignal) {
      this.parentAbortListener = () =>
        this.controller.abort(this.parentSignal?.reason);
      if (this.parentSignal.aborted)
        this.controller.abort(this.parentSignal.reason);
      else {
        this.parentSignal.addEventListener("abort", this.parentAbortListener, {
          once: true,
        });
      }
    } else {
      this.parentAbortListener = undefined;
    }
  }

  [Symbol.asyncIterator](): AsyncIterator<SSEEvent> {
    if (this.started)
      throw new Error("A Votrix EventStream can only be iterated once");
    if (this.closed) throw new Error("The Votrix EventStream is closed");
    this.started = true;
    return this.iterate();
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (this.parentAbortListener) {
      this.parentSignal?.removeEventListener("abort", this.parentAbortListener);
    }
    this.controller.abort(new Error("Event stream closed"));
    try {
      await this.response?.body?.cancel();
    } catch {
      // The response has already closed.
    } finally {
      this.response = null;
    }
  }

  private async *iterate(): AsyncGenerator<SSEEvent> {
    let lastEventID = this.headers.get("last-event-id");
    let reconnectDelayMS: number | null = null;
    let reconnects = 0;
    const rememberedIDs: string[] = [];
    const seenIDs = new Set<string>();

    try {
      while (!this.closed && !this.controller.signal.aborted) {
        try {
          this.response = await this.open(lastEventID);
          for await (const event of decodeSSE(
            this.response,
            this.controller.signal,
          )) {
            if (
              event.retry !== null &&
              event.retry !== undefined &&
              event.retry >= 0
            ) {
              reconnectDelayMS = event.retry;
            }
            if (event.sse_id !== null && event.sse_id !== undefined) {
              lastEventID = event.sse_id || null;
              if (event.sse_id && seenIDs.has(event.sse_id)) continue;
              if (event.sse_id) {
                rememberedIDs.push(event.sse_id);
                seenIDs.add(event.sse_id);
                if (rememberedIDs.length > 1024) {
                  const oldest = rememberedIDs.shift();
                  if (oldest !== undefined) seenIDs.delete(oldest);
                }
              }
            }
            yield event;
          }
        } catch (error) {
          if (
            error instanceof APIStreamError ||
            error instanceof APIStatusError
          )
            throw error;
          if (this.closed || this.controller.signal.aborted) return;
          if (reconnects >= this.maxReconnects) {
            if (error instanceof APIConnectionError) throw error;
            throw new APIStreamError("Votrix event stream disconnected", {
              cause: error,
            });
          }
        } finally {
          try {
            await this.response?.body?.cancel();
          } catch {
            // Normal EOF or transport teardown.
          }
          this.response = null;
        }

        if (
          this.closed ||
          this.controller.signal.aborted ||
          reconnects >= this.maxReconnects
        )
          return;
        await reconnectSleep(
          reconnects,
          reconnectDelayMS,
          this.controller.signal,
        );
        reconnects += 1;
      }
    } finally {
      await this.close();
    }
  }

  private async open(lastEventID: string | null): Promise<Response> {
    const headers = new Headers(this.headers);
    if (lastEventID) headers.set("last-event-id", lastEventID);
    else headers.delete("last-event-id");
    return await this.client.openStream(this.path, {
      query: this.query,
      headers,
      options: {
        ...this.requestOptions,
        signal: this.controller.signal,
      },
    });
  }
}

export async function* decodeSSE(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  let eventName: string | null = null;
  let eventID: string | null = null;
  let retry: number | null = null;
  let dataLines: string[] = [];

  const flush = (): SSEEvent | null => {
    if (dataLines.length === 0 && eventName === null && eventID === null)
      return null;
    const event = eventFromFrame(
      eventName,
      eventID,
      retry,
      dataLines.join("\n"),
    );
    eventName = null;
    eventID = null;
    retry = null;
    dataLines = [];
    return event;
  };

  for await (const line of responseLines(response, signal)) {
    if (line === "") {
      const event = flush();
      if (event) yield event;
      continue;
    }
    if (line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "id" && !value.includes("\0")) eventID = value;
    else if (field === "retry" && /^\d+$/.test(value)) retry = Number(value);
    else if (field === "data") dataLines.push(value);
  }
  const finalEvent = flush();
  if (finalEvent) yield finalEvent;
}

async function* responseLines(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<string> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const onAbort = (): void => {
    void reader.cancel(signal?.reason);
  };
  if (signal?.aborted) onAbort();
  else signal?.addEventListener("abort", onAbort, { once: true });
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        let line = buffer.slice(0, newline);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        buffer = buffer.slice(newline + 1);
        yield line;
        newline = buffer.indexOf("\n");
      }
    }
    buffer += decoder.decode();
    if (buffer) yield buffer.endsWith("\r") ? buffer.slice(0, -1) : buffer;
  } finally {
    signal?.removeEventListener("abort", onAbort);
    reader.releaseLock();
  }
}

function eventFromFrame(
  eventName: string | null,
  eventID: string | null,
  retry: number | null,
  rawData: string,
): SSEEvent {
  const decoded = decodeData(rawData);
  if (
    eventName === "error" ||
    (isRecord(decoded) && decoded.type === "error")
  ) {
    const nested =
      isRecord(decoded) && isRecord(decoded.error) ? decoded.error : undefined;
    const message =
      stringOrNull(nested?.message) ??
      (isRecord(decoded) ? stringOrNull(decoded.message) : null) ??
      "Votrix event stream failed";
    const errorType =
      stringOrNull(nested?.type) ??
      (isRecord(decoded) ? stringOrNull(decoded.type) : null);
    throw new APIStreamError(message, { errorType, requestID: eventID });
  }

  const payload: Record<string, unknown> = isRecord(decoded)
    ? { ...decoded }
    : {};
  const type =
    typeof payload.type === "string" ? payload.type : (eventName ?? "message");
  return {
    ...payload,
    type,
    event: typeof payload.event === "string" ? payload.event : eventName,
    sse_id: typeof payload.sse_id === "string" ? payload.sse_id : eventID,
    data: payload.data === undefined ? decoded : payload.data,
    raw_data: typeof payload.raw_data === "string" ? payload.raw_data : rawData,
    retry: typeof payload.retry === "number" ? payload.retry : retry,
  } as SSEEvent;
}

function decodeData(rawData: string): unknown {
  try {
    return JSON.parse(rawData) as unknown;
  } catch {
    return rawData;
  }
}

async function reconnectSleep(
  attempt: number,
  serverRetryMS: number | null,
  signal: AbortSignal,
): Promise<void> {
  const delay =
    serverRetryMS !== null
      ? Math.min(60_000, Math.max(0, serverRetryMS))
      : Math.min(8_000, 500 * 2 ** attempt) * (0.75 + Math.random() * 0.5);
  if (signal.aborted) return;
  await new Promise<void>((resolve) => {
    let settled = false;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      resolve();
    };
    const timer = setTimeout(finish, delay);
    timer.unref?.();
    const onAbort = (): void => {
      clearTimeout(timer);
      finish();
    };
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

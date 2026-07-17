import { APIPromise, type APIResponse } from "./api-promise.js";
import { VotrixError } from "./errors.js";
import type {
  APIClient,
  PageRequest,
  Query,
  RequestOptions,
  Sanitizer,
} from "./core.js";

export type CursorKind = "page" | "after_id" | "before_id";

export interface ListEnvelope<T> {
  data: T[];
  has_more: boolean;
  first_id: string | null;
  last_id: string | null;
  next_page: string | null;
}

interface PageState<T> {
  client: APIClient;
  path: string;
  envelope: ListEnvelope<T>;
  baseQuery: Query;
  cursor: CursorKind;
  options?: RequestOptions | undefined;
  sanitize?: Sanitizer<T> | undefined;
  currentCursor?: string | undefined;
  seenCursors?: ReadonlySet<string> | undefined;
}

/** A single cursor page that can lazily continue through the remaining pages. */
export class Page<T> implements ListEnvelope<T>, AsyncIterable<T> {
  readonly data: T[];
  readonly has_more: boolean;
  readonly first_id: string | null;
  readonly last_id: string | null;
  readonly next_page: string | null;

  private readonly client: APIClient;
  private readonly path: string;
  private readonly baseQuery: Query;
  private readonly cursor: CursorKind;
  private readonly options: RequestOptions | undefined;
  private readonly sanitize: Sanitizer<T> | undefined;
  private readonly seenCursors: Set<string>;

  constructor(state: PageState<T>) {
    this.client = state.client;
    this.path = state.path;
    this.baseQuery = state.baseQuery;
    this.cursor = state.cursor;
    this.options = state.options;
    this.sanitize = state.sanitize;
    this.data = state.envelope.data;
    this.has_more = state.envelope.has_more;
    this.first_id = state.envelope.first_id;
    this.last_id = state.envelope.last_id;
    this.next_page = state.envelope.next_page;
    this.seenCursors = new Set(state.seenCursors);
    if (state.currentCursor) this.seenCursors.add(state.currentCursor);
  }

  getPaginatedItems(): T[] {
    return this.data;
  }

  hasNextPage(): boolean {
    const cursor = this.nextCursor();
    return (
      this.has_more !== false &&
      cursor !== null &&
      !this.seenCursors.has(cursor)
    );
  }

  async getNextPage(): Promise<Page<T>> {
    const cursor = this.nextCursor();
    if (!this.hasNextPage() || cursor === null) {
      throw new VotrixError(
        "No next page expected; check page.hasNextPage() before calling page.getNextPage().",
      );
    }
    const query: Record<string, import("./core.js").QueryValue> = {
      ...this.baseQuery,
    };
    if (this.cursor === "after_id") delete query.before_id;
    if (this.cursor === "before_id") delete query.after_id;
    query[this.cursor] = cursor;
    const prepared = preparePage(
      this.client,
      this.path,
      {
        query,
        cursor: this.cursor,
        options: this.options,
        sanitize: this.sanitize,
      },
      cursor,
      this.seenCursors,
    );
    return await new APIPromise(prepared.response, prepared.parse);
  }

  async *iterPages(): AsyncGenerator<Page<T>> {
    let page: Page<T> = this;
    yield page;
    while (page.hasNextPage()) {
      page = await page.getNextPage();
      yield page;
    }
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<T> {
    const seenIDs = new Set<string>();
    for await (const page of this.iterPages()) {
      for (const item of page.data) {
        const id = itemIdentity(item);
        if (id && seenIDs.has(id)) continue;
        if (id) seenIDs.add(id);
        yield item;
      }
    }
  }

  toJSON(): ListEnvelope<T> {
    return {
      data: this.data,
      has_more: this.has_more,
      first_id: this.first_id,
      last_id: this.last_id,
      next_page: this.next_page,
    };
  }

  private nextCursor(): string | null {
    if (this.cursor === "after_id") return this.last_id;
    if (this.cursor === "before_id") return this.first_id;
    return this.next_page;
  }
}

/** Await for one page or iterate the unawaited value to auto-paginate. */
export class PagePromise<T>
  extends APIPromise<Page<T>>
  implements AsyncIterable<T>
{
  constructor(client: APIClient, path: string, request: PageRequest<T>) {
    const query = request.query ?? {};
    const initialCursor = queryValueAsString(query[request.cursor]);
    const prepared = preparePage(
      client,
      path,
      request,
      initialCursor ?? undefined,
    );
    super(prepared.response, prepared.parse);
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<T> {
    const page = await this;
    for await (const item of page) yield item;
  }
}

function preparePage<T>(
  client: APIClient,
  path: string,
  request: PageRequest<T>,
  currentCursor?: string,
  seenCursors?: ReadonlySet<string>,
): {
  response: Promise<Response>;
  parse: (response: Response) => Promise<APIResponse<Page<T>>>;
} {
  const query = request.query ?? {};
  const pending = client.request<ListEnvelope<T>>("GET", path, {
    query,
    options: request.options,
    sanitize: (value) => parseEnvelope(value, request.sanitize),
  });
  return {
    response: pending.asResponse(),
    parse: async () => {
      const { data, response, request_id } = await pending.withResponse();
      return {
        data: new Page<T>({
          client,
          path,
          envelope: data,
          baseQuery: query,
          cursor: request.cursor,
          options: request.options,
          sanitize: request.sanitize,
          currentCursor,
          seenCursors,
        }),
        response,
        requestID: request_id,
      };
    },
  };
}

function parseEnvelope<T>(
  value: unknown,
  sanitize?: Sanitizer<T>,
): ListEnvelope<T> {
  if (!isRecord(value) || !Array.isArray(value.data)) {
    throw new TypeError("Expected a Votrix list envelope");
  }
  return {
    data: value.data.map((item) => (sanitize ? sanitize(item) : (item as T))),
    has_more: typeof value.has_more === "boolean" ? value.has_more : false,
    first_id: stringOrNull(value.first_id),
    last_id: stringOrNull(value.last_id),
    next_page: stringOrNull(value.next_page),
  };
}

function queryValueAsString(
  value: import("./core.js").QueryValue,
): string | null {
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function itemIdentity(value: unknown): string | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  if (typeof value.version === "string" || typeof value.version === "number") {
    return `${value.id}\0${String(value.version)}`;
  }
  return value.id;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

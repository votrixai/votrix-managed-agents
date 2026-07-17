import type { APIClient, PagePromise, RequestOptions } from "../core.js";
import type { CursorKind } from "../pagination.js";
import type { UsageEntry, UsageListParams } from "../types.js";

const USAGE_PATH = "/v1/usage";
const PAGE_CURSOR: CursorKind = "page";

/** Organization-scoped, append-only raw usage facts. */
export class Usage {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  list(
    params: UsageListParams = {},
    options?: RequestOptions,
  ): PagePromise<UsageEntry> {
    return this.client.getPage<UsageEntry>(USAGE_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }
}

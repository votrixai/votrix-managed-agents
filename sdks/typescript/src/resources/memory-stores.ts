import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type {
  DeletedObject,
  Memory,
  MemoryCreateParams,
  MemoryDeleteParams,
  MemoryHistoryListParams,
  MemoryHistoryRetrieveParams,
  MemoryListItem,
  MemoryListParams,
  MemoryRetrieveByPathParams,
  MemoryRetrieveParams,
  MemoryStore,
  MemoryStoreCreateParams,
  MemoryStoreListParams,
  MemoryStoreUpdateParams,
  MemoryUpdateParams,
  MemoryVersion,
  MemoryVersionListParams,
  MemoryVersionRedactParams,
  MemoryVersionRetrieveParams,
} from "../types.js";

const MEMORY_STORES_PATH = "/v1/memory_stores";
const PAGE_CURSOR: CursorKind = "page";

export class MemoryStores {
  readonly memories: Memories;
  readonly memoryVersions: MemoryVersions;

  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
    this.memories = new Memories(client);
    this.memoryVersions = new MemoryVersions(client);
  }

  create(
    params: MemoryStoreCreateParams,
    options?: RequestOptions,
  ): APIPromise<MemoryStore> {
    return this.client.request<MemoryStore>("POST", MEMORY_STORES_PATH, {
      body: params,
      options,
    });
  }

  retrieve(
    memoryStoreID: string,
    options?: RequestOptions,
  ): APIPromise<MemoryStore> {
    return this.client.request<MemoryStore>(
      "GET",
      memoryStorePath(memoryStoreID),
      { options },
    );
  }

  update(
    memoryStoreID: string,
    params: MemoryStoreUpdateParams,
    options?: RequestOptions,
  ): APIPromise<MemoryStore> {
    return this.client.request<MemoryStore>(
      "POST",
      memoryStorePath(memoryStoreID),
      { body: params, options },
    );
  }

  list(
    params: MemoryStoreListParams = {},
    options?: RequestOptions,
  ): PagePromise<MemoryStore> {
    return this.client.getPage<MemoryStore>(MEMORY_STORES_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  archive(
    memoryStoreID: string,
    options?: RequestOptions,
  ): APIPromise<MemoryStore> {
    return this.client.request<MemoryStore>(
      "POST",
      `${memoryStorePath(memoryStoreID)}/archive`,
      { options },
    );
  }

  delete(
    memoryStoreID: string,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      memoryStorePath(memoryStoreID),
      { options },
    );
  }
}

export class Memories {
  readonly versions: MemoryHistory;

  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
    this.versions = new MemoryHistory(client);
  }

  create(
    memoryStoreID: string,
    params: MemoryCreateParams,
    options?: RequestOptions,
  ): APIPromise<Memory> {
    const { view, ...body } = params;
    return this.client.request<Memory>("POST", memoriesPath(memoryStoreID), {
      body,
      query: { view },
      options,
    });
  }

  retrieve(
    memoryID: string,
    params: MemoryRetrieveParams,
    options?: RequestOptions,
  ): APIPromise<Memory> {
    const { memory_store_id, ...query } = params;
    return this.client.request<Memory>(
      "GET",
      memoryPath(memoryID, memory_store_id),
      { query, options },
    );
  }

  retrieveByPath(
    memoryStoreID: string,
    params: MemoryRetrieveByPathParams,
    options?: RequestOptions,
  ): APIPromise<Memory> {
    return this.client.request<Memory>(
      "GET",
      `${memoriesPath(memoryStoreID)}/by_path`,
      { query: { ...params }, options },
    );
  }

  list(
    memoryStoreID: string,
    params: MemoryListParams = {},
    options?: RequestOptions,
  ): PagePromise<MemoryListItem> {
    return this.client.getPage<MemoryListItem>(memoriesPath(memoryStoreID), {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  update(
    memoryID: string,
    params: MemoryUpdateParams,
    options?: RequestOptions,
  ): APIPromise<Memory> {
    const { memory_store_id, view, ...body } = params;
    return this.client.request<Memory>(
      "POST",
      memoryPath(memoryID, memory_store_id),
      {
        body,
        query: { view },
        options,
      },
    );
  }

  delete(
    memoryID: string,
    params: MemoryDeleteParams,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    const { memory_store_id, ...query } = params;
    return this.client.request<DeletedObject>(
      "DELETE",
      memoryPath(memoryID, memory_store_id),
      { query, options },
    );
  }
}

/** Immutable versions for one Memory, addressed by numeric version. */
export class MemoryHistory {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  list(
    memoryID: string,
    params: MemoryHistoryListParams,
    options?: RequestOptions,
  ): PagePromise<MemoryVersion> {
    const { memory_store_id, ...query } = params;
    return this.client.getPage<MemoryVersion>(
      `${memoryPath(memoryID, memory_store_id)}/versions`,
      {
        query,
        cursor: PAGE_CURSOR,
        options,
      },
    );
  }

  retrieve(
    version: number,
    params: MemoryHistoryRetrieveParams,
    options?: RequestOptions,
  ): APIPromise<MemoryVersion> {
    const base = `${memoryPath(params.memory_id, params.memory_store_id)}/versions`;
    return this.client.request<MemoryVersion>(
      "GET",
      `${base}/${pathID(String(version), "version")}`,
      { options },
    );
  }
}

/** Store-wide immutable Memory versions, addressed by version ID. */
export class MemoryVersions {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  list(
    memoryStoreID: string,
    params: MemoryVersionListParams = {},
    options?: RequestOptions,
  ): PagePromise<MemoryVersion> {
    return this.client.getPage<MemoryVersion>(
      memoryVersionsPath(memoryStoreID),
      {
        query: { ...params },
        cursor: PAGE_CURSOR,
        options,
      },
    );
  }

  retrieve(
    memoryVersionID: string,
    params: MemoryVersionRetrieveParams,
    options?: RequestOptions,
  ): APIPromise<MemoryVersion> {
    const { memory_store_id, ...query } = params;
    return this.client.request<MemoryVersion>(
      "GET",
      memoryVersionPath(memoryVersionID, memory_store_id),
      { query, options },
    );
  }

  redact(
    memoryVersionID: string,
    params: MemoryVersionRedactParams,
    options?: RequestOptions,
  ): APIPromise<MemoryVersion> {
    return this.client.request<MemoryVersion>(
      "POST",
      `${memoryVersionPath(memoryVersionID, params.memory_store_id)}/redact`,
      { options },
    );
  }
}

function memoryStorePath(memoryStoreID: string): string {
  return `${MEMORY_STORES_PATH}/${pathID(memoryStoreID, "memoryStoreID")}`;
}

function memoriesPath(memoryStoreID: string): string {
  return `${memoryStorePath(memoryStoreID)}/memories`;
}

function memoryPath(memoryID: string, memoryStoreID: string): string {
  return `${memoriesPath(memoryStoreID)}/${pathID(memoryID, "memoryID")}`;
}

function memoryVersionsPath(memoryStoreID: string): string {
  return `${memoryStorePath(memoryStoreID)}/memory_versions`;
}

function memoryVersionPath(
  memoryVersionID: string,
  memoryStoreID: string,
): string {
  return `${memoryVersionsPath(memoryStoreID)}/${pathID(memoryVersionID, "memoryVersionID")}`;
}

function pathID(value: string, name: string): string {
  if (!value) throw new Error(`Expected a non-empty ${name}`);
  return encodeURIComponent(value);
}

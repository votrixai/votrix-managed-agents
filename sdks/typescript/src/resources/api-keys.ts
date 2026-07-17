import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type {
  ApiKey,
  ApiKeyCreateParams,
  ApiKeyCreated,
  ApiKeyListParams,
  ApiKeyRevokeParams,
  ApiKeyRotateParams,
} from "../types.js";

const API_KEYS_PATH = "/v1/api_keys";
const PAGE_CURSOR: CursorKind = "page";

export class ApiKeys {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  create(
    params: ApiKeyCreateParams,
    options?: RequestOptions,
  ): APIPromise<ApiKeyCreated> {
    return this.client.request<ApiKeyCreated>("POST", API_KEYS_PATH, {
      body: params,
      options,
      sanitize: (response) => this.client.sanitizeApiKey(response, true),
    });
  }

  list(
    params: ApiKeyListParams = {},
    options?: RequestOptions,
  ): PagePromise<ApiKey> {
    return this.client.getPage<ApiKey>(API_KEYS_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
      sanitize: (response) => this.client.sanitizeApiKey(response, false),
    });
  }

  retrieve(keyID: string, options?: RequestOptions): APIPromise<ApiKey> {
    return this.client.request<ApiKey>(
      "GET",
      `${API_KEYS_PATH}/${pathID(keyID, "keyID")}`,
      {
        options,
        sanitize: (response) => this.client.sanitizeApiKey(response, false),
      },
    );
  }

  revoke(
    keyID: string,
    params: ApiKeyRevokeParams = {},
    options?: RequestOptions,
  ): APIPromise<ApiKey> {
    return this.client.request<ApiKey>(
      "POST",
      `${API_KEYS_PATH}/${pathID(keyID, "keyID")}/revoke`,
      {
        body: params,
        options,
        sanitize: (response) => this.client.sanitizeApiKey(response, false),
      },
    );
  }

  rotate(
    keyID: string,
    params: ApiKeyRotateParams = {},
    options?: RequestOptions,
  ): APIPromise<ApiKeyCreated> {
    return this.client.request<ApiKeyCreated>(
      "POST",
      `${API_KEYS_PATH}/${pathID(keyID, "keyID")}/rotate`,
      {
        body: params,
        options,
        sanitize: (response) => this.client.sanitizeApiKey(response, true),
      },
    );
  }
}

function pathID(value: string, name: string): string {
  if (!value) {
    throw new Error(`Expected a non-empty ${name}`);
  }
  return encodeURIComponent(value);
}

import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type { ModelProvider } from "../types.js";

const MODEL_PROVIDERS_PATH = "/v1/model_providers";
const PAGE_CURSOR: CursorKind = "page";

export class ModelProviders {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  list(options?: RequestOptions): PagePromise<ModelProvider> {
    return this.client.getPage<ModelProvider>(MODEL_PROVIDERS_PATH, {
      cursor: PAGE_CURSOR,
      options,
    });
  }

  retrieve(
    providerID: string,
    options?: RequestOptions,
  ): APIPromise<ModelProvider> {
    return this.client.request<ModelProvider>(
      "GET",
      `${MODEL_PROVIDERS_PATH}/${pathID(providerID, "providerID")}`,
      { options },
    );
  }
}

function pathID(value: string, name: string): string {
  if (!value) throw new Error(`Expected a non-empty ${name}`);
  return encodeURIComponent(value);
}

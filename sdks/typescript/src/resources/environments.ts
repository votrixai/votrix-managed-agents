import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type {
  DeletedObject,
  Environment,
  EnvironmentCreateParams,
  EnvironmentListParams,
  EnvironmentUpdateParams,
} from "../types.js";

const ENVIRONMENTS_PATH = "/v1/environments";
const PAGE_CURSOR: CursorKind = "page";

export class Environments {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  create(
    params: EnvironmentCreateParams,
    options?: RequestOptions,
  ): APIPromise<Environment> {
    return this.client.request<Environment>("POST", ENVIRONMENTS_PATH, {
      body: params,
      options,
    });
  }

  retrieve(
    environmentID: string,
    options?: RequestOptions,
  ): APIPromise<Environment> {
    return this.client.request<Environment>(
      "GET",
      `${ENVIRONMENTS_PATH}/${pathID(environmentID, "environmentID")}`,
      { options },
    );
  }

  update(
    environmentID: string,
    params: EnvironmentUpdateParams,
    options?: RequestOptions,
  ): APIPromise<Environment> {
    return this.client.request<Environment>(
      "POST",
      `${ENVIRONMENTS_PATH}/${pathID(environmentID, "environmentID")}`,
      {
        body: params,
        options,
      },
    );
  }

  list(
    params: EnvironmentListParams = {},
    options?: RequestOptions,
  ): PagePromise<Environment> {
    return this.client.getPage<Environment>(ENVIRONMENTS_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  delete(
    environmentID: string,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      `${ENVIRONMENTS_PATH}/${pathID(environmentID, "environmentID")}`,
      { options },
    );
  }

  archive(
    environmentID: string,
    options?: RequestOptions,
  ): APIPromise<Environment> {
    return this.client.request<Environment>(
      "POST",
      `${ENVIRONMENTS_PATH}/${pathID(environmentID, "environmentID")}/archive`,
      { options },
    );
  }
}

function pathID(value: string, name: string): string {
  if (!value) {
    throw new Error(`Expected a non-empty ${name}`);
  }
  return encodeURIComponent(value);
}

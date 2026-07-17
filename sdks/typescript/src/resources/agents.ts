import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type {
  Agent,
  AgentCreateParams,
  AgentListParams,
  AgentRetrieveParams,
  AgentUpdateParams,
  AgentVersionListParams,
} from "../types.js";

const AGENTS_PATH = "/v1/agents";
const PAGE_CURSOR: CursorKind = "page";

export class Agents {
  readonly versions: AgentVersions;

  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
    this.versions = new AgentVersions(client);
  }

  create(
    params: AgentCreateParams,
    options?: RequestOptions,
  ): APIPromise<Agent> {
    return this.client.request<Agent>("POST", AGENTS_PATH, {
      body: params,
      options,
    });
  }

  retrieve(
    agentID: string,
    params: AgentRetrieveParams = {},
    options?: RequestOptions,
  ): APIPromise<Agent> {
    return this.client.request<Agent>(
      "GET",
      `${AGENTS_PATH}/${pathID(agentID, "agentID")}`,
      {
        query: { ...params },
        options,
      },
    );
  }

  update(
    agentID: string,
    params: AgentUpdateParams,
    options?: RequestOptions,
  ): APIPromise<Agent> {
    return this.client.request<Agent>(
      "POST",
      `${AGENTS_PATH}/${pathID(agentID, "agentID")}`,
      {
        body: params,
        options,
      },
    );
  }

  list(
    params: AgentListParams = {},
    options?: RequestOptions,
  ): PagePromise<Agent> {
    return this.client.getPage<Agent>(AGENTS_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  archive(agentID: string, options?: RequestOptions): APIPromise<Agent> {
    return this.client.request<Agent>(
      "POST",
      `${AGENTS_PATH}/${pathID(agentID, "agentID")}/archive`,
      { options },
    );
  }
}

export class AgentVersions {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  list(
    agentID: string,
    params: AgentVersionListParams = {},
    options?: RequestOptions,
  ): PagePromise<Agent> {
    return this.client.getPage<Agent>(
      `${AGENTS_PATH}/${pathID(agentID, "agentID")}/versions`,
      {
        query: { ...params },
        cursor: PAGE_CURSOR,
        options,
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

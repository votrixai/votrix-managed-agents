import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import { EventStream } from "../streaming.js";
import type {
  DeletedObject,
  SendEventsResult,
  Session,
  SessionCreateParams,
  SessionEvent,
  SessionEventListParams,
  SessionEventSendParams,
  SessionEventStreamParams,
  SessionListParams,
  SessionResource,
  SessionResourceAddParams,
  SessionResourceDeleteParams,
  SessionResourceListParams,
  SessionResourceRetrieveParams,
  SessionResourceUpdateParams,
  SessionUpdateParams,
} from "../types.js";

const SESSIONS_PATH = "/v1/sessions";
const PAGE_CURSOR: CursorKind = "page";

export class Sessions {
  readonly events: SessionEvents;
  readonly resources: SessionResources;

  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
    this.events = new SessionEvents(client);
    this.resources = new SessionResources(client);
  }

  create(
    params: SessionCreateParams,
    options?: RequestOptions,
  ): APIPromise<Session> {
    return this.client.request<Session>("POST", SESSIONS_PATH, {
      body: params,
      headers: { "Idempotency-Key": this.client.idempotencyKey(options) },
      options,
    });
  }

  retrieve(sessionID: string, options?: RequestOptions): APIPromise<Session> {
    return this.client.request<Session>(
      "GET",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}`,
      {
        options,
      },
    );
  }

  update(
    sessionID: string,
    params: SessionUpdateParams,
    options?: RequestOptions,
  ): APIPromise<Session> {
    return this.client.request<Session>(
      "POST",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}`,
      {
        body: params,
        options,
      },
    );
  }

  list(
    params: SessionListParams = {},
    options?: RequestOptions,
  ): PagePromise<Session> {
    return this.client.getPage<Session>(SESSIONS_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  delete(
    sessionID: string,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}`,
      { options },
    );
  }

  archive(sessionID: string, options?: RequestOptions): APIPromise<Session> {
    return this.client.request<Session>(
      "POST",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/archive`,
      { options },
    );
  }

  cancel(sessionID: string, options?: RequestOptions): APIPromise<Session> {
    return this.client.request<Session>(
      "POST",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/cancel`,
      { options },
    );
  }

  resume(sessionID: string, options?: RequestOptions): APIPromise<Session> {
    return this.client.request<Session>(
      "POST",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/resume`,
      { options },
    );
  }
}

export class SessionEvents {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  list(
    sessionID: string,
    params: SessionEventListParams = {},
    options?: RequestOptions,
  ): PagePromise<SessionEvent> {
    return this.client.getPage<SessionEvent>(
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/events`,
      {
        query: { ...params },
        cursor: PAGE_CURSOR,
        options,
      },
    );
  }

  send(
    sessionID: string,
    params: SessionEventSendParams,
    options?: RequestOptions,
  ): APIPromise<SendEventsResult> {
    return this.client.request<SendEventsResult>(
      "POST",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/events`,
      {
        body: params,
        headers: { "Idempotency-Key": this.client.idempotencyKey(options) },
        options,
      },
    );
  }

  stream(
    sessionID: string,
    params: SessionEventStreamParams = {},
    options?: RequestOptions,
  ): Promise<EventStream> {
    const { last_event_id, max_reconnects, ...query } = params;
    return this.client.stream(
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/events/stream`,
      {
        query: { ...query },
        headers:
          last_event_id === undefined
            ? undefined
            : { "Last-Event-ID": last_event_id },
        maxReconnects: max_reconnects,
        options,
      },
    );
  }
}

export class SessionResources {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  add(
    sessionID: string,
    params: SessionResourceAddParams,
    options?: RequestOptions,
  ): APIPromise<SessionResource> {
    if (params.type !== undefined && params.type !== "file") {
      throw new TypeError(
        "Session resources.add currently supports only type='file'",
      );
    }
    return this.client.request<SessionResource>(
      "POST",
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/resources`,
      {
        body: { ...params, type: "file" },
        options,
      },
    );
  }

  addFile(
    sessionID: string,
    params: Omit<SessionResourceAddParams, "type">,
    options?: RequestOptions,
  ): APIPromise<SessionResource> {
    return this.add(sessionID, params, options);
  }

  list(
    sessionID: string,
    params: SessionResourceListParams = {},
    options?: RequestOptions,
  ): PagePromise<SessionResource> {
    return this.client.getPage<SessionResource>(
      `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/resources`,
      {
        query: { ...params },
        cursor: PAGE_CURSOR,
        options,
      },
    );
  }

  retrieve(
    resourceID: string,
    params: SessionResourceRetrieveParams,
    options?: RequestOptions,
  ): APIPromise<SessionResource> {
    return this.client.request<SessionResource>(
      "GET",
      sessionResourcePath(resourceID, params.session_id),
      { options },
    );
  }

  update(
    resourceID: string,
    params: SessionResourceUpdateParams,
    options?: RequestOptions,
  ): APIPromise<SessionResource> {
    const { session_id, ...body } = params;
    return this.client.request<SessionResource>(
      "POST",
      sessionResourcePath(resourceID, session_id),
      {
        body,
        options,
      },
    );
  }

  delete(
    resourceID: string,
    params: SessionResourceDeleteParams,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      sessionResourcePath(resourceID, params.session_id),
      { options },
    );
  }
}

function sessionResourcePath(resourceID: string, sessionID: string): string {
  return `${SESSIONS_PATH}/${pathID(sessionID, "sessionID")}/resources/${pathID(resourceID, "resourceID")}`;
}

function pathID(value: string, name: string): string {
  if (!value) {
    throw new Error(`Expected a non-empty ${name}`);
  }
  return encodeURIComponent(value);
}

export interface APIResponse<T> {
  data: T;
  response: Response;
  requestID: string | null;
}

export type WithRequestID<T> = T extends object
  ? T & { readonly _request_id?: string | null }
  : T;

export interface WithResponse<T> {
  data: WithRequestID<T>;
  response: Response;
  request_id: string | null;
}

type ResponseParser<T> = (response: Response) => Promise<APIResponse<T>>;

/** A lazy Promise subclass with access to the underlying Fetch response. */
export class APIPromise<T> extends Promise<WithRequestID<T>> {
  private readonly rawResponsePromise: Promise<Response>;
  private readonly parser: ResponseParser<T>;
  private parsedResponsePromise: Promise<APIResponse<T>> | undefined;

  constructor(responsePromise: Promise<Response>, parser: ResponseParser<T>) {
    // Match Anthropic's lazy Promise subclass: the native promise state is a
    // no-op, while the overridden methods parse only when data is requested.
    super((resolve) => resolve(undefined as WithRequestID<T>));
    this.parser = parser;
    this.rawResponsePromise = responsePromise;
  }

  override then<TResult1 = WithRequestID<T>, TResult2 = never>(
    onfulfilled?:
      ((value: WithRequestID<T>) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    return this.parsed()
      .then((result) => result.data as WithRequestID<T>)
      .then(onfulfilled, onrejected);
  }

  override catch<TResult = never>(
    onrejected?: ((reason: unknown) => TResult | PromiseLike<TResult>) | null,
  ): Promise<WithRequestID<T> | TResult> {
    return this.then(undefined, onrejected);
  }

  override finally(onfinally?: (() => void) | null): Promise<WithRequestID<T>> {
    return this.parsed()
      .then((result) => result.data as WithRequestID<T>)
      .finally(onfinally ?? undefined);
  }

  async asResponse(): Promise<Response> {
    return await this.rawResponsePromise;
  }

  async withResponse(): Promise<WithResponse<T>> {
    const result = await this.parsed();
    return {
      data: result.data as WithRequestID<T>,
      response: result.response,
      request_id: result.requestID,
    };
  }

  private parsed(): Promise<APIResponse<T>> {
    if (!this.parsedResponsePromise) {
      this.parsedResponsePromise = this.rawResponsePromise.then(this.parser);
    }
    return this.parsedResponsePromise;
  }
}

export class VotrixError extends Error {
  override readonly name: string = "VotrixError";

  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class APIConnectionError extends VotrixError {
  override readonly name: string = "APIConnectionError";
}

export class APITimeoutError extends APIConnectionError {
  override readonly name: string = "APITimeoutError";
}

export interface APIResponseValidationErrorOptions {
  statusCode: number;
  requestID?: string | null;
  headers?: Headers;
  cause?: unknown;
}

export class APIResponseValidationError extends VotrixError {
  override readonly name: string = "APIResponseValidationError";
  readonly statusCode: number;
  readonly requestID: string | null;
  readonly headers: Headers;
  readonly rateLimitHeaders: Readonly<Record<string, string>>;

  constructor(message: string, options: APIResponseValidationErrorOptions) {
    super(message, { cause: options.cause });
    this.statusCode = options.statusCode;
    this.requestID = options.requestID ?? null;
    this.headers = options.headers ?? new Headers();
    this.rateLimitHeaders = collectRateLimitHeaders(this.headers);
  }
}

export interface APIStatusErrorOptions {
  statusCode: number;
  errorType?: string | null;
  errorCode?: string | null;
  requestID?: string | null;
  body?: unknown;
  response: Response;
}

export class APIStatusError extends VotrixError {
  override readonly name: string = "APIStatusError";
  readonly statusCode: number;
  readonly errorType: string | null;
  readonly errorCode: string | null;
  readonly requestID: string | null;
  readonly body: unknown;
  readonly response: Response;
  readonly headers: Headers;
  readonly retryAfter: string | null;
  readonly rateLimitHeaders: Readonly<Record<string, string>>;

  constructor(message: string, options: APIStatusErrorOptions) {
    super(message);
    this.statusCode = options.statusCode;
    this.errorType = options.errorType ?? null;
    this.errorCode = options.errorCode ?? null;
    this.requestID = options.requestID ?? null;
    this.body = options.body;
    this.response = options.response;
    this.headers = options.response.headers;
    this.retryAfter = options.response.headers.get("retry-after");
    this.rateLimitHeaders = collectRateLimitHeaders(options.response.headers);
  }
}

export class BadRequestError extends APIStatusError {
  override readonly name: string = "BadRequestError";
}

export class AuthenticationError extends APIStatusError {
  override readonly name: string = "AuthenticationError";
}

export class PermissionDeniedError extends APIStatusError {
  override readonly name: string = "PermissionDeniedError";
}

export class NotFoundError extends APIStatusError {
  override readonly name: string = "NotFoundError";
}

export class ConflictError extends APIStatusError {
  override readonly name: string = "ConflictError";
}

export class UnprocessableEntityError extends APIStatusError {
  override readonly name: string = "UnprocessableEntityError";
}

export class RateLimitError extends APIStatusError {
  override readonly name: string = "RateLimitError";
}

export class InternalServerError extends APIStatusError {
  override readonly name: string = "InternalServerError";
}

export interface APIStreamErrorOptions {
  errorType?: string | null;
  requestID?: string | null;
  cause?: unknown;
}

export class APIStreamError extends VotrixError {
  override readonly name: string = "APIStreamError";
  readonly errorType: string | null;
  readonly requestID: string | null;

  constructor(message: string, options: APIStreamErrorOptions = {}) {
    super(message, { cause: options.cause });
    this.errorType = options.errorType ?? null;
    this.requestID = options.requestID ?? null;
  }
}

export function statusErrorClass(statusCode: number): typeof APIStatusError {
  switch (statusCode) {
    case 400:
      return BadRequestError;
    case 401:
      return AuthenticationError;
    case 403:
      return PermissionDeniedError;
    case 404:
      return NotFoundError;
    case 409:
      return ConflictError;
    case 422:
      return UnprocessableEntityError;
    case 429:
      return RateLimitError;
    default:
      return statusCode >= 500 ? InternalServerError : APIStatusError;
  }
}

function collectRateLimitHeaders(
  headers: Headers,
): Readonly<Record<string, string>> {
  const values: Record<string, string> = {};
  headers.forEach((value, name) => {
    const normalized = name.toLowerCase();
    if (
      normalized.includes("ratelimit") ||
      normalized.includes("rate-limit") ||
      normalized === "retry-after"
    ) {
      values[name] = value;
    }
  });
  return values;
}

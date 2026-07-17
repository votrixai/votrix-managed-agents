import { basename, extname } from "node:path";
import { readFileSync } from "node:fs";

import type {
  APIClient,
  APIPromise,
  BinaryResponse,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type {
  DeletedObject,
  FileDownloadParams,
  FileListParams,
  FileObject,
  FileUploadParams,
  Uploadable,
  UploadFile,
} from "../types.js";

const FILES_PATH = "/v1/files";

export class Files {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  upload(
    params: FileUploadParams,
    options?: RequestOptions,
  ): APIPromise<FileObject> {
    const form = new FormData();
    form.append(
      "file",
      uploadableFile(params.file, {
        filename: params.filename,
        mimeType: params.mime_type,
      }),
    );
    return this.client.request<FileObject>("POST", FILES_PATH, {
      form,
      options,
    });
  }

  retrieveMetadata(
    fileID: string,
    options?: RequestOptions,
  ): APIPromise<FileObject> {
    return this.client.request<FileObject>(
      "GET",
      `${FILES_PATH}/${pathID(fileID, "fileID")}`,
      {
        options,
      },
    );
  }

  list(
    params: FileListParams = {},
    options?: RequestOptions,
  ): PagePromise<FileObject> {
    if (params.after_id !== undefined && params.before_id !== undefined) {
      throw new TypeError("Pass either after_id or before_id, not both");
    }
    const cursor: CursorKind =
      params.before_id === undefined ? "after_id" : "before_id";
    return this.client.getPage<FileObject>(FILES_PATH, {
      query: { ...params },
      cursor,
      options,
    });
  }

  download(
    fileID: string,
    params: FileDownloadParams = {},
    options?: RequestOptions,
  ): APIPromise<BinaryResponse> {
    // BinaryResponse is always streaming-first; `stream` remains accepted for
    // parity with the Python and Anthropic-shaped clients.
    void params.stream;
    return this.client.binary(
      `${FILES_PATH}/${pathID(fileID, "fileID")}/content`,
      { options },
    );
  }

  delete(fileID: string, options?: RequestOptions): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      `${FILES_PATH}/${pathID(fileID, "fileID")}`,
      { options },
    );
  }
}

interface UploadOverrides {
  filename?: string | undefined;
  mimeType?: string | undefined;
  forceMetadata?: boolean;
}

export function uploadableFile(
  value: Uploadable | UploadFile,
  overrides: UploadOverrides = {},
): File {
  const descriptor = isUploadFile(value) ? value : undefined;
  const upload = descriptor?.data ?? value;
  const forced = overrides.forceMetadata === true;
  const filename = nonEmpty(
    forced ? overrides.filename : descriptor?.filename,
    forced ? descriptor?.filename : overrides.filename,
    upload instanceof File ? upload.name : undefined,
    typeof upload === "string" ? basename(upload) : undefined,
    "upload",
  );
  const mimeType = nonEmpty(
    forced ? overrides.mimeType : descriptor?.mime_type,
    forced ? descriptor?.mime_type : overrides.mimeType,
    upload instanceof Blob ? upload.type : undefined,
    guessMimeType(filename),
    "application/octet-stream",
  );

  if (
    upload instanceof File &&
    upload.name === filename &&
    upload.type === mimeType
  ) {
    return upload;
  }
  if (typeof upload === "string") {
    return new File([readFileSync(upload)], filename, { type: mimeType });
  }
  if (upload instanceof Blob) {
    return new File([upload], filename, { type: mimeType });
  }
  if (upload instanceof ArrayBuffer) {
    return new File([upload], filename, { type: mimeType });
  }
  if (ArrayBuffer.isView(upload)) {
    const bytes = new Uint8Array(
      upload.buffer,
      upload.byteOffset,
      upload.byteLength,
    );
    return new File([new Uint8Array(bytes)], filename, { type: mimeType });
  }
  throw new TypeError(
    "Expected an uploadable filesystem path, Blob, ArrayBuffer, or typed array",
  );
}

function isUploadFile(value: Uploadable | UploadFile): value is UploadFile {
  return typeof value === "object" && value !== null && "data" in value;
}

function nonEmpty(...values: Array<string | undefined>): string {
  return (
    values.find((value) => typeof value === "string" && value.length > 0) ?? ""
  );
}

function guessMimeType(filename: string): string | undefined {
  return MIME_TYPES[extname(filename).toLowerCase()];
}

const MIME_TYPES: Readonly<Record<string, string>> = {
  ".gif": "image/gif",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".json": "application/json",
  ".md": "text/markdown",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".txt": "text/plain",
  ".webp": "image/webp",
  ".zip": "application/zip",
};

function pathID(value: string, name: string): string {
  if (!value) throw new Error(`Expected a non-empty ${name}`);
  return encodeURIComponent(value);
}

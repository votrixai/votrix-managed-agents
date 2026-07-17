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
  Skill,
  SkillCreateParams,
  SkillFileInput,
  SkillListParams,
  SkillVersion,
  SkillVersionCreateParams,
  SkillVersionDeleteParams,
  SkillVersionDownloadParams,
  SkillVersionListParams,
  SkillVersionRetrieveParams,
  UploadFile,
} from "../types.js";
import { uploadableFile } from "./files.js";

const SKILLS_PATH = "/v1/skills";
const PAGE_CURSOR: CursorKind = "page";

export class Skills {
  readonly versions: SkillVersions;

  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
    this.versions = new SkillVersions(client);
  }

  create(
    params: SkillCreateParams,
    options?: RequestOptions,
  ): APIPromise<Skill> {
    const request = skillContentRequest(params, true);
    return this.client.request<Skill>("POST", SKILLS_PATH, {
      ...request,
      options,
    });
  }

  retrieve(skillID: string, options?: RequestOptions): APIPromise<Skill> {
    return this.client.request<Skill>(
      "GET",
      `${SKILLS_PATH}/${pathID(skillID, "skillID")}`,
      {
        options,
      },
    );
  }

  list(
    params: SkillListParams = {},
    options?: RequestOptions,
  ): PagePromise<Skill> {
    return this.client.getPage<Skill>(SKILLS_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  delete(skillID: string, options?: RequestOptions): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      `${SKILLS_PATH}/${pathID(skillID, "skillID")}`,
      { options },
    );
  }
}

export class SkillVersions {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  create(
    skillID: string,
    params: SkillVersionCreateParams,
    options?: RequestOptions,
  ): APIPromise<SkillVersion> {
    const request = skillContentRequest(params, true);
    return this.client.request<SkillVersion>(
      "POST",
      `${SKILLS_PATH}/${pathID(skillID, "skillID")}/versions`,
      { ...request, options },
    );
  }

  retrieve(
    version: number,
    params: SkillVersionRetrieveParams,
    options?: RequestOptions,
  ): APIPromise<SkillVersion> {
    return this.client.request<SkillVersion>(
      "GET",
      skillVersionPath(version, params.skill_id),
      { options },
    );
  }

  list(
    skillID: string,
    params: SkillVersionListParams = {},
    options?: RequestOptions,
  ): PagePromise<SkillVersion> {
    return this.client.getPage<SkillVersion>(
      `${SKILLS_PATH}/${pathID(skillID, "skillID")}/versions`,
      {
        query: { ...params },
        cursor: PAGE_CURSOR,
        options,
      },
    );
  }

  download(
    version: number,
    params: SkillVersionDownloadParams,
    options?: RequestOptions,
  ): APIPromise<BinaryResponse> {
    void params.stream;
    return this.client.binary(
      `${skillVersionPath(version, params.skill_id)}/content`,
      {
        options,
      },
    );
  }

  delete(
    version: number,
    params: SkillVersionDeleteParams,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      skillVersionPath(version, params.skill_id),
      { options },
    );
  }
}

function skillContentRequest(
  params: SkillCreateParams | SkillVersionCreateParams,
  includeDisplayTitle: boolean,
): { body?: unknown; form?: FormData } {
  const { archive, files, ...metadata } = params;
  if (archive !== undefined && files !== undefined) {
    throw new TypeError("Pass either archive or files, not both");
  }
  if (archive === undefined && (!files || files.length === 0)) {
    throw new TypeError("A skill requires archive or files");
  }

  if (archive !== undefined) {
    const form = new FormData();
    form.append(
      "files",
      uploadableFile(archive, {
        filename: "skill.zip",
        mimeType: "application/zip",
        forceMetadata: true,
      }),
    );
    appendDisplayTitle(form, metadata, includeDisplayTitle);
    return { form };
  }

  if (files && multipartMode(files)) {
    const form = new FormData();
    for (const file of files) form.append("files", uploadableFile(file));
    appendDisplayTitle(form, metadata, includeDisplayTitle);
    return { form };
  }

  return { body: { ...metadata, files } };
}

function multipartMode(
  files: readonly SkillFileInput[] | readonly UploadFile[],
): files is readonly UploadFile[] {
  const uploadCount = files.filter(isUploadFile).length;
  if (uploadCount !== 0 && uploadCount !== files.length) {
    throw new TypeError(
      "Skill files must be all JSON file objects or all multipart uploads",
    );
  }
  return uploadCount === files.length;
}

function isUploadFile(value: SkillFileInput | UploadFile): value is UploadFile {
  return typeof value === "object" && value !== null && "data" in value;
}

function appendDisplayTitle(
  form: FormData,
  metadata: Record<string, unknown>,
  include: boolean,
): void {
  if (!include) return;
  const displayTitle = metadata.display_title;
  if (typeof displayTitle === "string")
    form.append("display_title", displayTitle);
}

function skillVersionPath(version: number, skillID: string): string {
  return `${SKILLS_PATH}/${pathID(skillID, "skillID")}/versions/${pathID(String(version), "version")}`;
}

function pathID(value: string, name: string): string {
  if (!value) throw new Error(`Expected a non-empty ${name}`);
  return encodeURIComponent(value);
}

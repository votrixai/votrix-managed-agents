import type {
  APIClient,
  APIPromise,
  PagePromise,
  RequestOptions,
} from "../core.js";
import type { CursorKind } from "../pagination.js";
import type {
  DeletedObject,
  ModelCredential,
  ModelCredentialArchiveParams,
  ModelCredentialCreateParams,
  ModelCredentialDeleteParams,
  ModelCredentialListParams,
  ModelCredentialRetrieveParams,
  ModelCredentialRotateParams,
  Vault,
  VaultCreateParams,
  VaultListParams,
  VaultUpdateParams,
} from "../types.js";

const VAULTS_PATH = "/v1/vaults";
const PAGE_CURSOR: CursorKind = "page";

export class Vaults {
  readonly modelCredentials: ModelCredentials;

  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
    this.modelCredentials = new ModelCredentials(client);
  }

  create(
    params: VaultCreateParams,
    options?: RequestOptions,
  ): APIPromise<Vault> {
    return this.client.request<Vault>("POST", VAULTS_PATH, {
      body: params,
      options,
    });
  }

  retrieve(vaultID: string, options?: RequestOptions): APIPromise<Vault> {
    return this.client.request<Vault>(
      "GET",
      `${VAULTS_PATH}/${pathID(vaultID, "vaultID")}`,
      {
        options,
      },
    );
  }

  update(
    vaultID: string,
    params: VaultUpdateParams,
    options?: RequestOptions,
  ): APIPromise<Vault> {
    return this.client.request<Vault>(
      "POST",
      `${VAULTS_PATH}/${pathID(vaultID, "vaultID")}`,
      {
        body: params,
        options,
      },
    );
  }

  list(
    params: VaultListParams = {},
    options?: RequestOptions,
  ): PagePromise<Vault> {
    return this.client.getPage<Vault>(VAULTS_PATH, {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
    });
  }

  archive(vaultID: string, options?: RequestOptions): APIPromise<Vault> {
    return this.client.request<Vault>(
      "POST",
      `${VAULTS_PATH}/${pathID(vaultID, "vaultID")}/archive`,
      { options },
    );
  }

  delete(vaultID: string, options?: RequestOptions): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      `${VAULTS_PATH}/${pathID(vaultID, "vaultID")}`,
      { options },
    );
  }
}

export class ModelCredentials {
  private readonly client: APIClient;

  constructor(client: APIClient) {
    this.client = client;
  }

  create(
    vaultID: string,
    params: ModelCredentialCreateParams,
    options?: RequestOptions,
  ): APIPromise<ModelCredential> {
    return this.client.request<ModelCredential>(
      "POST",
      modelCredentialsPath(vaultID),
      {
        body: params,
        options,
        sanitize: (response) => this.client.sanitizeModelCredential(response),
      },
    );
  }

  list(
    vaultID: string,
    params: ModelCredentialListParams = {},
    options?: RequestOptions,
  ): PagePromise<ModelCredential> {
    return this.client.getPage<ModelCredential>(modelCredentialsPath(vaultID), {
      query: { ...params },
      cursor: PAGE_CURSOR,
      options,
      sanitize: (response) => this.client.sanitizeModelCredential(response),
    });
  }

  retrieve(
    credentialID: string,
    params: ModelCredentialRetrieveParams,
    options?: RequestOptions,
  ): APIPromise<ModelCredential> {
    return this.client.request<ModelCredential>(
      "GET",
      modelCredentialPath(credentialID, params.vault_id),
      {
        options,
        sanitize: (response) => this.client.sanitizeModelCredential(response),
      },
    );
  }

  rotate(
    vaultID: string,
    credentialID: string,
    params: ModelCredentialRotateParams,
    options?: RequestOptions,
  ): APIPromise<ModelCredential> {
    return this.client.request<ModelCredential>(
      "POST",
      modelCredentialPath(credentialID, vaultID),
      {
        body: params,
        options,
        sanitize: (response) => this.client.sanitizeModelCredential(response),
      },
    );
  }

  archive(
    credentialID: string,
    params: ModelCredentialArchiveParams,
    options?: RequestOptions,
  ): APIPromise<ModelCredential> {
    return this.client.request<ModelCredential>(
      "POST",
      `${modelCredentialPath(credentialID, params.vault_id)}/archive`,
      {
        options,
        sanitize: (response) => this.client.sanitizeModelCredential(response),
      },
    );
  }

  delete(
    credentialID: string,
    params: ModelCredentialDeleteParams,
    options?: RequestOptions,
  ): APIPromise<DeletedObject> {
    return this.client.request<DeletedObject>(
      "DELETE",
      modelCredentialPath(credentialID, params.vault_id),
      { options },
    );
  }
}

function modelCredentialsPath(vaultID: string): string {
  return `${VAULTS_PATH}/${pathID(vaultID, "vaultID")}/model_credentials`;
}

function modelCredentialPath(credentialID: string, vaultID: string): string {
  return `${modelCredentialsPath(vaultID)}/${pathID(credentialID, "credentialID")}`;
}

function pathID(value: string, name: string): string {
  if (!value) throw new Error(`Expected a non-empty ${name}`);
  return encodeURIComponent(value);
}

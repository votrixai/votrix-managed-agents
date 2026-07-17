/**
 * Public wire types for the Votrix Managed Agents API.
 *
 * Resource methods use camelCase in TypeScript, while every property below
 * deliberately keeps the API's snake_case spelling. Dates remain RFC 3339
 * strings and are not converted to JavaScript Date objects.
 */

export interface OpenObject {
  [key: string]: unknown;
}

/** Bounded resource metadata accepted by Agent/Environment/Session APIs. */
export type Metadata = Record<string, string>;
export type MetadataPatch = Record<string, string | null>;
/** Arbitrary JSON metadata used only where the API explicitly permits it. */
export type JSONMetadata = Record<string, unknown>;
export type StringMetadata = Record<string, string>;

export interface ListEnvelope<T> {
  data: T[];
  has_more?: boolean;
  first_id?: string | null;
  last_id?: string | null;
  next_page?: string | null;
}

export interface SSEEvent extends OpenObject {
  type: string;
  seq?: number | null;
  event?: string | null;
  id?: string | null;
  sse_id?: string | null;
  data?: unknown;
  raw_data: string;
  retry?: number | null;
}

export interface DeletedObject extends OpenObject {
  id: string;
  type: string;
  deleted: true;
}

// API keys

export type ApiKeyScope = "api" | "api_keys:manage" | "worker";

interface ApiKeyFields {
  id: string;
  type: "api_key";
  organization_id: string;
  name: string;
  prefix: string;
  scopes: ApiKeyScope[];
  expires_at: string | null;
  created_by: string | null;
  metadata: JSONMetadata;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  revocation_reason: string | null;
  replaced_by_key_id: string | null;
  replaces_key_id: string | null;
  created_at: string;
  updated_at: string;
}

/** Safe metadata returned by list, retrieve, revoke, and other read paths. */
export type ApiKey = ApiKeyFields & {
  secret?: never;
};

/** The one-time create or rotate response containing the plaintext key. */
export type ApiKeyCreated = ApiKeyFields & {
  secret: string;
};

export interface ApiKeyCreateParams {
  name: string;
  scopes?: readonly ApiKeyScope[];
  expires_at?: string | null;
  metadata?: JSONMetadata;
}

export interface ApiKeyListParams {
  limit?: number;
  page?: string;
  include_revoked?: boolean;
}

export interface ApiKeyRevokeParams {
  reason?: string | null;
}

export interface ApiKeyRotateParams {
  expires_at?: string | null;
  reason?: string | null;
}

// Agents

export interface ModelSpec extends OpenObject {
  id: string;
  provider?: string | null;
}

export interface AgentModelOptions {
  speed?: "standard" | "fast" | null;
  provider?: string | null;
  provider_id?: string | null;
  vendor?: string | null;
  source?: string | null;
}

export type AgentModelObject = AgentModelOptions &
  ({ id: string; model?: string } | { id?: string; model: string });

export type AgentModelInput = string | AgentModelObject;

export interface Agent extends OpenObject {
  id: string;
  type: "agent";
  name: string;
  version: number;
  model: ModelSpec;
  system?: string | null;
  description?: string | null;
  tools: OpenObject[];
  mcp_servers: OpenObject[];
  skills: OpenObject[];
  multiagent?: OpenObject | null;
  metadata: Metadata;
  archived_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentReference {
  type?: "agent";
  id: string;
  version?: number | null;
}

export interface AgentWithOverrides {
  type: "agent_with_overrides";
  id: string;
  version?: number | null;
  model?: AgentModelInput | null;
  system?: string | null;
  tools?: readonly OpenObject[];
  mcp_servers?: readonly OpenObject[];
  skills?: readonly OpenObject[];
}

export interface AgentCreateParams {
  name: string;
  model: AgentModelInput;
  system?: string | null;
  description?: string | null;
  tools?: readonly OpenObject[];
  mcp_servers?: readonly OpenObject[];
  skills?: readonly OpenObject[];
  multiagent?: OpenObject | null;
  metadata?: Metadata;
  runtime?: OpenObject;
}

export interface AgentRetrieveParams {
  version?: number;
}

export interface AgentUpdateParams {
  version: number;
  name?: string;
  model?: AgentModelInput;
  system?: string | null;
  description?: string | null;
  tools?: readonly OpenObject[] | null;
  mcp_servers?: readonly OpenObject[] | null;
  skills?: readonly OpenObject[] | null;
  multiagent?: OpenObject | null;
  metadata?: MetadataPatch | null;
  runtime?: OpenObject | null;
}

export interface AgentListParams {
  limit?: number;
  page?: string;
  include_archived?: boolean;
  "created_at[gte]"?: string;
  "created_at[lte]"?: string;
}

export interface AgentVersionListParams {
  limit?: number;
  page?: string;
}

// Environments

export type EnvironmentScope = "organization" | "account";

export interface Environment extends OpenObject {
  id: string;
  type: "environment";
  name: string;
  description: string;
  config: OpenObject;
  scope?: EnvironmentScope | null;
  metadata: Metadata;
  archived_at?: string | null;
  deleted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface EnvironmentCreateParams {
  name: string;
  config?: OpenObject | null;
  description?: string | null;
  metadata?: Metadata;
  scope?: EnvironmentScope | null;
}

export interface EnvironmentUpdateParams {
  name?: string | null;
  description?: string | null;
  config?: OpenObject | null;
  metadata?: MetadataPatch | null;
  scope?: EnvironmentScope | null;
}

export interface EnvironmentListParams {
  limit?: number;
  page?: string;
  include_archived?: boolean;
}

// Sessions, events, and attached resources

export interface SessionFileResourceInput {
  type: "file";
  file_id: string;
  mount_path?: string | null;
}

/** Public-beta Session creation accepts uploaded file resources. */
export type SessionResourceInput = SessionFileResourceInput;

export interface SessionFileResource extends OpenObject {
  id: string;
  type: "file";
  file_id: string;
  mount_path: string;
  created_at: string;
  updated_at: string;
}

export interface SessionGithubResource extends OpenObject {
  id: string;
  type: "github_repository";
  url: string;
  mount_path: string;
  checkout?: Record<string, string> | null;
  created_at: string;
  updated_at: string;
}

export interface SessionMemoryResource extends OpenObject {
  id: string;
  type: "memory_store";
  memory_store_id: string;
  access: "read_only" | "read_write";
  description: string;
  mount_path?: string | null;
  name?: string | null;
  instructions?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionGenericResource extends OpenObject {
  id: string;
  type: string;
  created_at: string;
  updated_at: string;
}

/** Includes legacy response shapes so old Sessions remain readable. */
export type SessionResource =
  | SessionFileResource
  | SessionGithubResource
  | SessionMemoryResource
  | SessionGenericResource;

export interface SessionAgentSnapshot extends OpenObject {
  id: string;
  type: "agent";
  name: string;
  version: number;
  model: ModelSpec;
  system?: string | null;
  description?: string | null;
  tools: OpenObject[];
  mcp_servers: OpenObject[];
  skills: OpenObject[];
  multiagent?: OpenObject | null;
}

export interface Session extends OpenObject {
  id: string;
  type: "session";
  agent?: SessionAgentSnapshot | null;
  agent_id: string;
  agent_version: number;
  environment_id: string;
  title?: string | null;
  status: string;
  status_details: JSONMetadata;
  stop_reason?: OpenObject | null;
  run_state?: OpenObject | null;
  sandbox_state?: OpenObject | null;
  metadata: Metadata;
  resources: SessionResource[];
  outcome_evaluations: OpenObject[];
  stats: JSONMetadata;
  usage: JSONMetadata;
  vault_ids: string[];
  last_event_seq: number;
  archived_at?: string | null;
  deleted_at?: string | null;
  deployment_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionCreateParams {
  agent: string | AgentReference | AgentWithOverrides;
  environment_id: string;
  title?: string | null;
  metadata?: Metadata;
  resources?: readonly SessionResourceInput[];
  vault_ids?: readonly string[];
}

export interface SessionUpdateParams {
  title?: string | null;
  metadata?: MetadataPatch | null;
  agent?: OpenObject | null;
}

export interface SessionListParams {
  limit?: number;
  page?: string;
  include_archived?: boolean;
  order?: "asc" | "desc";
  agent_id?: string;
  agent_version?: number;
  memory_store_id?: string;
  deployment_id?: string;
  statuses?: readonly string[];
  "created_at[gt]"?: string;
  "created_at[gte]"?: string;
  "created_at[lt]"?: string;
  "created_at[lte]"?: string;
}

export interface SessionEventInput extends OpenObject {
  type: string;
}

export interface SessionEvent extends OpenObject {
  id: string;
  type: string;
  session_id: string;
  seq: number;
  created_at: string;
  processed_at?: string | null;
}

export interface SendEventsResult {
  data: SessionEvent[];
}

export interface SessionEventSendParams {
  events: readonly SessionEventInput[];
}

export interface SessionEventListParams {
  after_seq?: number;
  limit?: number;
  page?: string;
  order?: "asc" | "desc";
  types?: readonly string[];
  "created_at[gt]"?: string;
  "created_at[gte]"?: string;
  "created_at[lt]"?: string;
  "created_at[lte]"?: string;
}

export interface SessionEventStreamParams {
  after_seq?: number;
  event_deltas?: readonly string[];
  last_event_id?: string;
  max_reconnects?: number;
}

export interface SessionResourceAddParams {
  type?: "file";
  file_id: string;
  mount_path?: string | null;
}

export interface SessionResourceListParams {
  limit?: number;
  page?: string;
}

export interface SessionResourceRetrieveParams {
  session_id: string;
}

export interface SessionResourceUpdateParams {
  session_id: string;
  authorization_token: string;
}

export interface SessionResourceDeleteParams {
  session_id: string;
}

// Files

/** A filesystem path, Blob, Buffer/typed-array view, or ArrayBuffer. */
export type Uploadable = string | Blob | ArrayBuffer | ArrayBufferView;

export interface UploadFile {
  data: Uploadable;
  filename?: string;
  mime_type?: string;
}

export interface FileScope extends OpenObject {
  type: "session";
  id: string;
}

export interface FileObject extends OpenObject {
  id: string;
  type: "file";
  name?: string | null;
  filename?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  deduplicated_from_file_id?: string | null;
  scope?: FileScope | null;
  status?: string | null;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  deleted_at?: string | null;
}

export interface FileUploadParams {
  file: Uploadable | UploadFile;
  filename?: string;
  mime_type?: string;
}

export interface FileListParams {
  limit?: number;
  after_id?: string;
  before_id?: string;
  scope_id?: string;
}

export interface FileDownloadParams {
  stream?: boolean;
}

// Skills

export interface SkillFileInput extends OpenObject {
  filename: string;
  content: string | readonly number[];
  mime_type?: string | null;
}

export interface SkillFile extends OpenObject {
  filename: string;
  mime_type?: string | null;
  size_bytes: number;
}

export interface SkillVersion extends OpenObject {
  id: string;
  type: "skill_version";
  skill_id: string;
  version: string;
  name: string;
  description: string;
  directory: string;
  top_level_directory?: string | null;
  files: SkillFile[];
  manifest?: OpenObject | null;
  archive_format?: string | null;
  filename?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  deleted_at?: string | null;
}

export interface Skill extends OpenObject {
  id: string;
  type: "skill";
  name?: string | null;
  display_title?: string | null;
  description?: string | null;
  top_level_directory?: string | null;
  latest_version?: string | number | null;
  source: "anthropic" | "custom";
  version?: SkillVersion | null;
  status?: string | null;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  deleted_at?: string | null;
}

interface SkillCreateMetadata extends OpenObject {
  display_title?: string | null;
  name?: string | null;
  description?: string | null;
}

type SkillContentParams =
  | {
      archive: Uploadable | UploadFile;
      files?: never;
    }
  | {
      archive?: never;
      files: readonly SkillFileInput[] | readonly UploadFile[];
    };

export type SkillCreateParams = SkillCreateMetadata & SkillContentParams;

export interface SkillListParams {
  limit?: number;
  page?: string;
  source?: "anthropic" | "custom";
}

export type SkillVersionCreateParams = OpenObject & SkillContentParams;

export interface SkillVersionListParams {
  limit?: number;
  page?: string;
}

export interface SkillVersionRetrieveParams {
  skill_id: string;
}

export interface SkillVersionDownloadParams {
  skill_id: string;
  stream?: boolean;
}

export interface SkillVersionDeleteParams {
  skill_id: string;
}

// Vaults and native provider credentials

export interface Vault extends OpenObject {
  id: string;
  type: "vault";
  name: string;
  display_name: string;
  metadata: StringMetadata;
  status: "active" | "archived";
  archived_at?: string | null;
  deleted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface VaultCreateParams {
  display_name: string;
  metadata?: StringMetadata;
}

export interface VaultUpdateParams {
  display_name?: string | null;
  metadata?: Record<string, string | null> | null;
}

export interface VaultListParams {
  limit?: number;
  page?: string;
  include_archived?: boolean;
}

/**
 * Provider credential metadata. Write-only key material and internal auth
 * mappings are intentionally impossible to express on this response type.
 */
export interface ModelCredential {
  id: string;
  type: "model_credential";
  vault_id: string;
  model_provider: string;
  display_name: string;
  metadata: StringMetadata;
  archived_at?: string | null;
  created_at: string;
  updated_at: string;
  api_key?: never;
  auth?: never;
  secret_name?: never;
}

export interface ModelCredentialCreateParams {
  provider: string;
  api_key: string;
  display_name?: string | null;
  metadata?: StringMetadata;
}

export interface ModelCredentialListParams {
  limit?: number;
  page?: string;
  include_archived?: boolean;
}

export interface ModelCredentialRetrieveParams {
  vault_id: string;
}

export interface ModelCredentialRotateParams {
  api_key: string;
}

export interface ModelCredentialArchiveParams {
  vault_id: string;
}

export interface ModelCredentialDeleteParams {
  vault_id: string;
}

// Model-provider catalog

export interface ProviderCapabilities extends OpenObject {
  streaming: boolean;
  tool_calls: boolean;
  multimodal_input: boolean;
  reasoning: boolean;
  native_structured_output: boolean;
}

export interface ModelProvider extends OpenObject {
  id: string;
  type: "model_provider";
  display_name: string;
  adapter: string;
  credential_type: "api_key" | "none" | string;
  default_model?: string | null;
  capabilities: ProviderCapabilities;
}

export type ModelProviderListParams = Readonly<Record<string, never>>;

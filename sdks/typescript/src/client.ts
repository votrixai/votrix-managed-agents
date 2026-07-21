import { APIClient, type VotrixOptions } from "./core.js";
import { Agents } from "./resources/agents.js";
import { ApiKeys } from "./resources/api-keys.js";
import { Environments } from "./resources/environments.js";
import { Files } from "./resources/files.js";
import { MemoryStores } from "./resources/memory-stores.js";
import { ModelProviders } from "./resources/model-providers.js";
import { Sessions } from "./resources/sessions.js";
import { Skills } from "./resources/skills.js";
import { Usage } from "./resources/usage.js";
import { Vaults } from "./resources/vaults.js";

/** Promise-based, server-side client for the native Votrix Managed Agents API. */
export class Votrix extends APIClient {
  readonly apiKeys: ApiKeys;
  readonly agents: Agents;
  readonly environments: Environments;
  readonly sessions: Sessions;
  readonly files: Files;
  readonly memoryStores: MemoryStores;
  readonly skills: Skills;
  readonly vaults: Vaults;
  readonly modelProviders: ModelProviders;
  readonly usage: Usage;

  constructor(options: VotrixOptions = {}) {
    super(options);
    this.apiKeys = new ApiKeys(this);
    this.agents = new Agents(this);
    this.environments = new Environments(this);
    this.sessions = new Sessions(this);
    this.files = new Files(this);
    this.memoryStores = new MemoryStores(this);
    this.skills = new Skills(this);
    this.vaults = new Vaults(this);
    this.modelProviders = new ModelProviders(this);
    this.usage = new Usage(this);
  }
}

export default Votrix;

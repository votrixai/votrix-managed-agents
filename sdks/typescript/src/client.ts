import { APIClient, type VotrixOptions } from "./core.js";
import { Agents } from "./resources/agents.js";
import { ApiKeys } from "./resources/api-keys.js";
import { Environments } from "./resources/environments.js";
import { Files } from "./resources/files.js";
import { ModelProviders } from "./resources/model-providers.js";
import { Sessions } from "./resources/sessions.js";
import { Skills } from "./resources/skills.js";
import { Vaults } from "./resources/vaults.js";

/** Promise-based, server-side client for the native Votrix Managed Agents API. */
export class Votrix extends APIClient {
  readonly apiKeys: ApiKeys;
  readonly agents: Agents;
  readonly environments: Environments;
  readonly sessions: Sessions;
  readonly files: Files;
  readonly skills: Skills;
  readonly vaults: Vaults;
  readonly modelProviders: ModelProviders;

  constructor(options: VotrixOptions = {}) {
    super(options);
    this.apiKeys = new ApiKeys(this);
    this.agents = new Agents(this);
    this.environments = new Environments(this);
    this.sessions = new Sessions(this);
    this.files = new Files(this);
    this.skills = new Skills(this);
    this.vaults = new Vaults(this);
    this.modelProviders = new ModelProviders(this);
  }
}

export default Votrix;

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    database_url: str = "sqlite+aiosqlite:///./votrix_managed_agents.db"
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_use_responses: bool = False
    deepseek_api_key: str = ""
    deepseek_api_base: str = ""

    vma_api_key: str = ""
    vma_api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    vma_allow_anonymous_local: bool = True
    vma_require_beta_header: bool = True
    vma_require_anthropic_version_header: bool = True
    vma_default_model_provider: str = "anthropic"
    vma_default_anthropic_model: str = "claude-sonnet-4-6"
    vma_default_openai_model: str = "gpt-5.5"
    vma_default_deepseek_model: str = "deepseek-chat"
    vma_model_providers: Annotated[dict[str, dict[str, Any]], NoDecode] = Field(default_factory=dict)
    vma_checkpoint_database_url: str = ""
    vma_sandbox_provider: str = "state"
    vma_sandbox_factory: str = ""
    vma_allow_unsafe_local_sandbox: bool = False
    vma_sandbox_root: str = "/workspace"
    vma_sandbox_retention_seconds: int = 30 * 24 * 60 * 60
    vma_sandbox_janitor_interval_seconds: int = 60
    vma_sandbox_command_timeout_seconds: int = 900
    vma_e2b_template: str = ""
    vma_e2b_workdir: str = "/workspace"
    vma_e2b_guest_user: str = "user"
    vma_e2b_timeout_seconds: int = 900
    vma_e2b_auto_pause: bool = True
    vma_e2b_auto_resume: bool = False
    vma_e2b_keep_memory: bool = True
    vma_e2b_pause_on_exit: bool = True
    vma_e2b_allow_public_traffic: bool = False
    vma_e2b_template_resources: Annotated[dict[str, Any], NoDecode] = Field(default_factory=dict)
    vma_max_graph_steps: int = 250
    vma_run_timeout_seconds: int = 900
    vma_web_fetch_max_bytes: int = 1_000_000
    vma_web_search_endpoint: str = ""
    vma_web_allow_private_networks: bool = False
    vma_default_workspace_id: str = "wrkspc_default"
    vma_api_key_workspaces: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    vma_event_poll_interval_seconds: float = 0.5
    vma_max_file_upload_bytes: int = 50 * 1024 * 1024
    vma_max_skill_archive_bytes: int = 25 * 1024 * 1024
    vma_public_base_url: str = "https://example.invalid"
    vma_worker_token: str = ""
    vma_encryption_key: str = ""
    vma_allow_plaintext_secrets_local: bool = True

    e2b_api_key: str = ""
    e2b_domain: str = ""
    e2b_api_url: str = ""
    e2b_sandbox_url: str = ""

    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = ""
    s3_public_url: str = ""
    s3_region: str = "auto"

    app_env: str = "local"
    sentry_dsn: str = ""
    log_level: str = "INFO"

    @field_validator("vma_api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("vma_model_providers", mode="before")
    @classmethod
    def parse_model_providers(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_validator("vma_api_key_workspaces", mode="before")
    @classmethod
    def parse_api_key_workspaces(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_validator("vma_e2b_template_resources", mode="before")
    @classmethod
    def parse_e2b_template_resources(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return json.loads(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()

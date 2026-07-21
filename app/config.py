import json
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal

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
    vma_service_role: Literal["combined", "api", "worker"] = "combined"
    vma_db_pool_size: int = Field(default=10, ge=0)
    vma_db_max_overflow: int = Field(default=5, ge=0)
    vma_db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    vma_db_pool_recycle_seconds: int = Field(default=300, ge=-1)
    anthropic_base_url: str = ""
    openai_base_url: str = ""
    openai_use_responses: bool = False
    deepseek_api_base: str = ""

    vma_require_beta_header: bool = True
    vma_require_anthropic_version_header: bool = True
    vma_default_model_provider: str = "anthropic"
    vma_default_anthropic_model: str = "claude-sonnet-4-6"
    vma_default_openai_model: str = "gpt-5.5"
    vma_default_deepseek_model: str = "deepseek-chat"
    vma_model_providers: Annotated[dict[str, dict[str, Any]], NoDecode] = Field(default_factory=dict)
    vma_checkpoint_database_url: str = ""
    vma_listen_database_url: str = ""
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
    vma_e2b_timeout_seconds: int = 300
    vma_e2b_auto_pause: bool = True
    vma_e2b_auto_resume: bool = False
    vma_e2b_keep_memory: bool = False
    vma_e2b_pause_on_exit: bool = True
    vma_e2b_allow_public_traffic: bool = False
    vma_e2b_template_resources: Annotated[dict[str, Any], NoDecode] = Field(default_factory=dict)
    vma_e2b_cost_estimation_enabled: bool = True
    vma_e2b_vcpu_second_usd: Decimal = Field(default=Decimal("0.000014"), ge=0)
    vma_e2b_gib_second_usd: Decimal = Field(default=Decimal("0.0000045"), ge=0)
    vma_max_graph_steps: int = 250
    vma_run_timeout_seconds: int = 900
    vma_web_fetch_max_bytes: int = 1_000_000
    vma_web_search_endpoint: str = ""
    vma_web_allow_private_networks: bool = False
    vma_event_poll_interval_seconds: float = 0.5
    vma_max_file_upload_bytes: int = 50 * 1024 * 1024
    vma_max_session_input_bytes: int = 64 * 1024 * 1024
    vma_max_skill_archive_bytes: int = 25 * 1024 * 1024
    vma_embedded_worker_enabled: bool = False
    vma_worker_concurrency: int = 1
    vma_worker_poll_interval_seconds: float = 0.5
    vma_worker_lease_seconds: int = 120
    vma_work_max_attempts: int = Field(default=3, ge=0)
    vma_work_dispatch_mode: Literal["poll", "hybrid"] = "poll"
    vma_tasks_queue: str = ""
    vma_tasks_location: str = ""
    vma_tasks_service_account: str = ""
    vma_worker_url: str = ""
    vma_worker_turn_limit: int = Field(default=5, ge=1)
    vma_checkpoint_pool_max_size: int = Field(default=5, ge=1)
    vma_preview_broker: Literal["process_local", "pg_notify"] = "process_local"
    vma_cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    vma_public_ga_only: bool = False
    vma_public_build_id: str = Field(
        default="dev",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    vma_governance_enabled: bool = True
    vma_requests_per_minute: int = Field(default=120, ge=0)
    vma_max_active_work: int = Field(default=5, ge=0)
    vma_daily_model_tokens: int = Field(default=1_000_000, ge=0)
    vma_organization_storage_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=0)
    vma_encryption_key: str = ""
    vma_allow_plaintext_secrets_local: bool = True
    vma_supabase_url: str = ""
    vma_supabase_publishable_key: str = ""

    e2b_api_key: str = ""
    e2b_domain: str = ""
    e2b_api_url: str = ""
    e2b_sandbox_url: str = ""

    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = ""
    s3_region: str = "auto"

    app_env: str = "local"
    sentry_dsn: str = ""
    log_level: str = "INFO"

    @field_validator("vma_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("vma_public_build_id", mode="before")
    @classmethod
    def normalize_public_build_id(cls, value):
        if value is None:
            return "dev"
        return str(value).strip() or "dev"

    @field_validator("vma_model_providers", mode="before")
    @classmethod
    def parse_model_providers(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            embedded = [
                str(name)
                for name, config in value.items()
                if isinstance(config, dict) and config.get("api_key") not in (None, "")
            ]
            if embedded:
                raise ValueError(
                    "VMA_MODEL_PROVIDERS must not embed model API keys; "
                    "create a model Credential in an Organization Vault"
                )
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

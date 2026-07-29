from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets, and the one thing that differs between running this locally and
    running it deployed.

    Nothing else belongs here. Limits, paths and image names are decisions this
    codebase has made rather than knobs an operator turns, so they live as
    constants beside the code that reads them — a setting nobody is meant to
    change is just a way to break the service from a file nobody reviews.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./votrix_managed_agents.db"

    # Platform keys, one per provider. Callers name a model; they never supply
    # credentials, and no key is shared between providers.
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""

    # Object storage (Cloudflare R2 speaks the S3 API).
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = ""

    e2b_api_key: str = ""
    # How long a container may sit idle before E2B pauses it. Long enough to
    # cover a model thinking between tool calls; short enough that an abandoned
    # session is not billed all afternoon.
    sandbox_timeout_seconds: int = 900
    sandbox_command_timeout_seconds: int = 300
    # Long enough for a container to finish a transfer, short enough that a URL
    # in a log is worth nothing by the time anyone reads it.
    transfer_url_ttl_seconds: int = 600
    # An agent can fill a disk. Collection takes what looks like deliverables
    # rather than uploading whatever it finds.
    max_output_files: int = 50
    max_output_bytes: int = 100 * 1024 * 1024

    # How a turn gets run once a message is accepted. `inline` runs it inside
    # the request, which is slow but needs no infrastructure — the default so a
    # fresh clone works with no configuration. Deployments set `cloud`.
    turn_dispatch: Literal["cloud", "inline"] = "inline"

    # Only read when `turn_dispatch` is "cloud", and then all of them are
    # required. Where the queue lives, who the task authenticates as, and the
    # address it calls back on — none of which this service can work out.
    tasks_project: str = ""
    tasks_location: str = ""
    tasks_queue: str = ""
    tasks_service_account: str = ""
    worker_url: str = ""

    @model_validator(mode="after")
    def _cloud_dispatch_is_fully_configured(self) -> "Settings":
        """Refuse to start half-configured.

        A missing queue name would otherwise surface as a message that was
        accepted, committed, and then never run by anyone.
        """
        if self.turn_dispatch != "cloud":
            return self
        missing = [
            name
            for name in (
                "tasks_project",
                "tasks_location",
                "tasks_queue",
                "tasks_service_account",
                "worker_url",
            )
            if not getattr(self, name).strip()
        ]
        if missing:
            raise ValueError(
                "TURN_DISPATCH=cloud requires: " + ", ".join(n.upper() for n in missing)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

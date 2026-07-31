from functools import lru_cache
import re
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
    database_schema: str = ""

    # Platform keys, one per provider. Callers name a model; they never supply
    # credentials, and no key is shared between providers.
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""
    openai_api_key: str = ""

    # Object storage (Cloudflare R2 speaks the S3 API).
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = ""

    # How many Postgres connections to keep open and hand round.
    #
    # Zero means `NullPool`: a fresh connection per database session, which
    # against a hosted Postgres is a TCP handshake, a TLS handshake and an
    # authentication round trip every time — 1.9 seconds against the Supabase
    # pooler in us-east-2, versus 0.9 for the same work on a connection that
    # already exists.
    #
    # The ceiling is not ours. That pooler, in session mode, refuses the
    # sixteenth client outright:
    #
    #   (EMAXCONNSESSION) max clients reached in session mode
    #   - max clients are limited to pool_size: 15
    #
    # And this pool is not the only thing spending that budget: LangGraph's
    # checkpointer opens its own psycopg connection per turn, outside here
    # entirely. `pool_size + max_overflow` plus one per concurrent turn has to
    # stay under fifteen, so these two are deliberately well below it. Raising
    # them does not buy throughput; it buys a failure at the far end.
    vma_db_pool_size: int = 5
    vma_db_max_overflow: int = 5
    vma_db_pool_timeout_seconds: float = 10.0
    vma_db_pool_recycle_seconds: int = 300
    # On, and it has to be. It was briefly off — a pre-ping measured 1.3s per
    # checkout, which looked like more than the pooling it protects saves, and
    # recycling on age was meant to cover the gap. It did not: the pooler in
    # front of this database drops idle connections well inside the recycle
    # window, and the live suite went from clean to thirty `connection is
    # closed` failures in one run. A connection handed out dead is a failed
    # request; a connection tested first is a slow one.
    #
    # The 1.3s is itself a symptom rather than the price of a ping — a ping is
    # one round trip, and one round trip here is 150ms. What the rest of it
    # measures is pre-ping finding a dead connection and rebuilding it, which
    # is work that has to happen either way.
    vma_db_pool_pre_ping: bool = True

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
        if self.database_schema and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.database_schema
        ):
            raise ValueError("DATABASE_SCHEMA must be a valid PostgreSQL identifier")
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

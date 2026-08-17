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

    app_env: str = "local"
    vma_public_build_id: str = "dev"
    vma_git_commit_sha: str = ""

    # Comma-separated browser origins allowed to call the API from a page.
    #
    # This is not an authorization boundary. CORS only governs what a browser
    # will let one site's JavaScript read from another, and a server-side caller
    # is unaffected by this list entirely. Empty means no browser origin is
    # allowed. Authentication still happens independently through an
    # Organization API key or the first-party Console's verified user identity
    # and membership.
    vma_cors_origins: str = ""

    # First-party Console identity. Public API consumers still authenticate
    # with Organization API keys; these values let VMA independently verify a
    # Console user's Supabase access token before resolving their membership.
    vma_supabase_url: str = ""
    vma_supabase_publishable_key: str = ""

    database_url: str = "sqlite+aiosqlite:///./votrix_managed_agents.db"
    database_schema: str = ""
    # LangGraph uses a dedicated, session-affine psycopg connection outside
    # SQLAlchemy's transaction pool. The runtime also sets search_path
    # explicitly after connecting because Supabase's pooler drops the startup
    # `options=-csearch_path=...` parameter.
    vma_checkpoint_database_url: str = ""
    # A session-mode DSN (`:5432`) for the `LISTEN` that wakes open streams. A
    # transaction pooler cannot carry notifications — it connects, and then
    # silently delivers nothing, which is why the listener self-tests before it
    # reports itself ready. Empty disables the wake-up entirely and every stream
    # polls instead, which is what local runs and the worker service both do.
    vma_listen_database_url: str = ""

    # Mints the per-Account keys, and the only provider credential this
    # deployment holds — every key that can actually be spent belongs to an
    # Account and is stored encrypted. The provider refuses inference on a
    # provisioning key, so a leak of this one cannot be spent, only used to
    # enumerate and revoke.
    openrouter_management_key: str = ""

    # Holds every turn to one upstream provider behind the gateway, named by
    # its slug (`deepseek`, `fireworks`, `together`, …). Empty — the deployed
    # value — leaves routing to the gateway, which is right in production and
    # ruinous for a measurement: consecutive turns land on different hosts, so
    # the spread being timed is the routing, not the model. Set it only to hold
    # the upstream still, and only for as long as that is what is being asked.
    openrouter_provider_only: str = ""

    # How long streamed text is held before it becomes an event.
    #
    # What this really sets is how much of a database connection each in-flight
    # turn holds. A flush borrows a connection for a checkout ping, the
    # `UPDATE ... RETURNING` that allocates the seq, the insert and the commit —
    # four round trips, so ~100ms at the 25ms the deployed pooler answers in.
    # Against a 250ms window that is half a connection per turn, and this pool
    # hands out ten while the worker is allowed twenty concurrent turns: deltas
    # alone would claim every connection the service has, and they are not the
    # only thing asking. Agent messages, session state and an interrupt all
    # queue behind them. Doubling the window halves the claim.
    #
    # It does not go higher because the window is also the floor on how often a
    # short reply appears to move. A hundred-character answer flushes three or
    # four times at 0.5s and once or twice at 1s, and something that arrives in
    # two pieces is not streaming. Going lower buys nothing anyone can see: at
    # 80 tokens/s a 0.5s window carries about sixty characters, which is already
    # an order of magnitude past reading speed.
    #
    # A knob rather than a constant because the cost of a flush is the round
    # trip to the database, and that is environment, not code: deployed beside
    # Postgres it is milliseconds, while a laptop reaching a hosted database
    # pays hundreds and needs a much longer window to keep turns responsive.
    vma_delta_flush_seconds: float = 0.5

    # Base64url AES-256 key wrapping provider secrets at rest. Without it no
    # Account credential can be written or read, which is why provisioning
    # fails loudly rather than storing a key in the clear.
    vma_encryption_key: str = ""

    # Server-side credential used by the web_search and web_fetch tools.
    firecrawl_api_key: str = ""

    # Object storage (Cloudflare R2 speaks the S3 API).
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = ""

    # How many Postgres connections to keep open and hand round.
    #
    # Zero means `NullPool`: a fresh connection per database session, which
    # against a hosted Postgres is a TCP handshake, a TLS handshake and an
    # authentication round trip every time. The gap grows when developers are
    # far from the shared Supabase region, so local Postgres runs should keep a
    # bounded pool instead of reconnecting for every session.
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

    # LangGraph's checkpoint traffic, pooled the same way and for the same
    # reasons. Sized small on purpose: a turn borrows a connection per
    # checkpoint call and gives it straight back, so this caps concurrent
    # checkpoint statements, not concurrent turns.
    vma_checkpoint_pool_max_size: int = 3
    vma_checkpoint_pool_max_lifetime_seconds: float = 300.0

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

    # Run the janitor (app/worker.py) inside this process, as a background loop.
    #
    # Off by default because most processes must not: it is one sweep of the
    # whole tenant's stranded sessions per minute. Hosted workers enable it so
    # each Cloud Tasks cold start also starts the loop. With `minScale: 0` it is
    # intentionally best-effort between requests: the Session lease remains
    # authoritative, and the next message can reclaim an expired lease even if
    # no sweep ran while the worker was scaled to zero.
    #
    # Safe on more than one instance, but only because the sweep selects its
    # batch `FOR UPDATE SKIP LOCKED` (see `list_stuck_sessions`). Without that
    # two sweepers would read the same stranded session and both write it an
    # error event. `maxScale` on the worker is 4, so this is not hypothetical.
    vma_run_sweeper: bool = False

    @property
    def cors_origins(self) -> tuple[str, ...]:
        """The configured origins, in order, without blanks or duplicates."""
        seen: dict[str, None] = {}
        for origin in self.vma_cors_origins.split(","):
            trimmed = origin.strip()
            if trimmed:
                seen.setdefault(trimmed, None)
        return tuple(seen)

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

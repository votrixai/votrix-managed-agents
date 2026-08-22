"""one table for every container, whoever owns it

Revision ID: b8d4f1a92c73
Revises: f6c8d2a91e74
Create Date: 2026-08-22

`session_sandboxes` recorded the container a conversation lives in. Containers
held directly through `/v1/sandbox` are the same row with a different owner —
same provider, same columns, same class driving them — so they share a table,
and `session_id` says which sort a row is. A `kind` column would be a second
copy of what the foreign key already states.

The table is renamed rather than rebuilt: the rows in it are live. A view under
the old name stays behind so that a release still running when this lands keeps
reading and writing exactly as before. Migrations here are a separate Cloud Run
job, so there is always such a release; without the view every session
operation would fail between the job finishing and the new revision serving.

Dropping that view is the follow-up, once no old revision is left.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f1a92c73"
down_revision: Union[str, None] = "f6c8d2a91e74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# What the old release writes when it inserts through the view. Every column
# added below has to have a default, or those inserts fail on a NOT NULL it
# does not know exists.
_SESSION_TTL_SECONDS = "900"

_STATES = "'provisioning', 'running', 'paused', 'terminated', 'failed'"

# Only the columns the old release knows about. Naming them rather than using
# `SELECT *` is what keeps the view stable when later migrations add more.
_VIEW_COLUMNS = (
    "id, organization_id, session_id, provider, external_sandbox_id, state, "
    "expires_at, last_active_at, error, lock_version, created_at, updated_at"
)


def _old_table() -> sa.Table:
    """The table as it stands, spelled out rather than reflected.

    SQLite keeps check constraints in the table's DDL text and gives them back
    unnamed, which a batch rebuild cannot re-create. Handing the definition in
    is what lets one code path serve both backends: Postgres takes the native
    ALTERs below and never rebuilds anything.
    """
    return sa.Table(
        "session_sandboxes",
        sa.MetaData(),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey(
                "sessions.id", ondelete="CASCADE", name="fk_sandboxes_session"
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_sandbox_id", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_active_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.JSON()),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_session_sandboxes_session"),
        sa.CheckConstraint(
            "state IN ('provisioning', 'running', 'paused', 'terminated', 'failed')",
            name="ck_session_sandboxes_state",
        ),
    )


def upgrade() -> None:
    # Additive first, while the table still has its old name: on SQLite a
    # batch alter rebuilds the table, which would break a view pointing at it.
    with op.batch_alter_table("session_sandboxes", copy_from=_old_table()) as batch:
        batch.add_column(
            sa.Column(
                "environment_id",
                sa.String(64),
                sa.ForeignKey("environments.id", name="fk_sandboxes_environment"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "ttl_seconds",
                sa.Integer(),
                nullable=False,
                server_default=_SESSION_TTL_SECONDS,
            )
        )
        batch.add_column(
            sa.Column(
                "network_access",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        # A container held through the API has no conversation.
        batch.alter_column("session_id", existing_type=sa.String(64), nullable=True)
        batch.drop_constraint("uq_session_sandboxes_session", type_="unique")
        batch.drop_constraint("ck_session_sandboxes_state", type_="check")
        batch.create_check_constraint("ck_sandboxes_state", f"state IN ({_STATES})")
        batch.create_check_constraint("ck_sandboxes_ttl_positive", "ttl_seconds > 0")

    # Which image each existing container started from. Not derivable later:
    # the session could be gone by then.
    op.execute(
        "UPDATE session_sandboxes SET environment_id = ("
        "  SELECT environment_id FROM sessions WHERE sessions.id ="
        "         session_sandboxes.session_id"
        ")"
    )

    # Postgres carries an index through a rename under its old name, while a
    # SQLite batch rebuild has already dropped it. Dropping it explicitly is
    # what stops the two backends ending up with different schemas — the
    # composite index below covers what it did.
    op.execute("DROP INDEX IF EXISTS ix_session_sandboxes_organization_id")

    op.rename_table("session_sandboxes", "sandboxes")

    # One container per conversation, and any number belonging to none. A
    # plain unique constraint would allow a single API-held row in the whole
    # table, because those all have a NULL session.
    op.create_index(
        "uq_sandboxes_session",
        "sandboxes",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("session_id IS NOT NULL"),
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    op.create_index(
        "ix_sandboxes_organization_state", "sandboxes", ["organization_id", "state"]
    )

    # A single-table view with no aggregate is updatable in Postgres, so the
    # release still running keeps working unchanged.
    op.execute(
        f"CREATE VIEW session_sandboxes AS SELECT {_VIEW_COLUMNS} FROM sandboxes"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS session_sandboxes")
    op.drop_index("ix_sandboxes_organization_state", table_name="sandboxes")
    op.drop_index("uq_sandboxes_session", table_name="sandboxes")
    op.rename_table("sandboxes", "session_sandboxes")

    # Rows without a session cannot be represented by the old shape at all.
    op.execute("DELETE FROM session_sandboxes WHERE session_id IS NULL")

    op.create_index(
        "ix_session_sandboxes_organization_id",
        "session_sandboxes",
        ["organization_id"],
    )

    with op.batch_alter_table("session_sandboxes") as batch:
        batch.drop_constraint("ck_sandboxes_ttl_positive", type_="check")
        batch.drop_constraint("ck_sandboxes_state", type_="check")
        batch.create_check_constraint(
            "ck_session_sandboxes_state",
            "state IN ('provisioning', 'running', 'paused', 'terminated', 'failed')",
        )
        batch.create_unique_constraint("uq_session_sandboxes_session", ["session_id"])
        batch.alter_column("session_id", existing_type=sa.String(64), nullable=False)
        batch.drop_column("network_access")
        batch.drop_column("ttl_seconds")
        batch.drop_column("environment_id")

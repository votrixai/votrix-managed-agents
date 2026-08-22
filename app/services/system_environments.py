"""Images VMA ships, as opposed to images a caller declared.

An ordinary environment is a list of packages: the caller says what to install
and the provider builds it. That covers most things and needs nothing here.

It does not cover an image whose build has to *check something* — that the
rules it bakes in are the version we meant, that the thing it installed
actually runs. `infra/e2b/hf_lint` fails its own build when the linter's rule
count is not what the checked-in constant says, and no list of package names
can express that.

So these images are built from a template in this repository, promoted by
hand, and named here. A caller asks for one by slug and never sees a template
name or an image id.

The row is still per-organization, because everything downstream — the
concurrency cap, the file scopes, `list` — is written in terms of one tenant's
environments, and a shared row would be a hole in exactly that. Only the image
is shared, which costs nothing: templates are global at the provider and the
image is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Environment
from app.db.models.environments import READY
from app.db.queries import environments as environments_q
from app.models.errors import InvalidRequest

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SystemEnvironment:
    slug: str
    # The provider's unversioned template tag. Unversioned on purpose: which
    # build it points at is moved by hand once a candidate is proven, and
    # every container started afterwards picks it up without a deploy.
    template: str
    description: str


SYSTEM_ENVIRONMENTS: dict[str, SystemEnvironment] = {
    "hf-lint": SystemEnvironment(
        slug="hf-lint",
        template="votrix-hf-lint",
        description=(
            "HyperFrames composition rules. Checks that a project archive is "
            "one HeyGen will render before anyone pays it to try."
        ),
    ),
}

# What the environment row is called. The prefix is what makes it findable
# again, and what keeps it from colliding with a name a caller chose.
_NAME_PREFIX = "system:"


def name_for(slug: str) -> str:
    return f"{_NAME_PREFIX}{slug}"


async def resolve(
    db: AsyncSession,
    *,
    organization_id: str,
    slug: str,
) -> Environment:
    """The organization's row for a system image, made the first time it asks.

    Created rather than seeded at startup: an organization that never runs one
    of these should not have rows for all of them, and a new slug should not
    need a migration.
    """
    known = SYSTEM_ENVIRONMENTS.get(slug)
    if known is None:
        available = ", ".join(sorted(SYSTEM_ENVIRONMENTS)) or "none"
        raise InvalidRequest(
            f"Unknown system environment {slug!r}. Available: {available}"
        )

    existing = await environments_q.get_environment_by_name(
        db, organization_id=organization_id, name=name_for(slug)
    )
    if existing is not None:
        return existing

    environment = await environments_q.create_environment(
        db,
        organization_id=organization_id,
        name=name_for(slug),
        description=known.description,
        config={"system_environment": slug},
        # Nothing to build: the image already exists at the provider, promoted
        # from `infra/e2b/`. Ready is the truth, not an optimistic default.
        image_id=known.template,
        build_state=READY,
    )
    await db.commit()
    logger.info(
        "system_environment_created",
        slug=slug,
        environment_id=environment.id,
        organization_id=organization_id,
    )
    return environment


__all__ = ["SYSTEM_ENVIRONMENTS", "SystemEnvironment", "name_for", "resolve"]

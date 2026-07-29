"""Environment use cases.

An environment is an image recipe. Declaring packages starts a build at the
sandbox provider, which takes minutes — so creating one returns immediately and
the build is asked about later, because nothing calls us when it finishes.

Sessions each start a fresh container from that image. Packages go in once at
build time rather than on every session, which is the difference between a
sub-second start and a two-minute one.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Environment
from app.db.models.environments import BUILDING, FAILED, READY
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import environments as environments_q
from app.models.environments import EnvironmentConfig
from app.models.errors import Conflict, NotFound
from app.utils.sandbox import Image


async def create_environment(
    db: AsyncSession,
    *,
    organization_id: str,
    name: str,
    description: str | None = None,
    config: dict[str, Any] | None = None,
) -> Environment:
    """Register an environment and, if it declares packages, start its build."""
    existing = await environments_q.get_environment_by_name(
        db, name=name, organization_id=organization_id
    )
    if existing is not None:
        raise Conflict(f"An environment named {name!r} already exists")

    config = config or {}
    packages = _packages(config)
    environment = await environments_q.create_environment(
        db,
        organization_id=organization_id,
        name=name,
        description=description,
        config=config,
        # Nothing declared means nothing to build: sessions start from the base
        # image, with no wait and no image of their own. It is written down
        # rather than worked out later, so which image a session ran on is a
        # stored fact and not something the sandbox layer has to guess.
        image_id=None if packages else Image.base().image_id,
        build_state=BUILDING if packages else READY,
    )
    if packages:
        await _start_build(db, environment, config)
    await db.commit()
    return environment


async def get_environment(
    db: AsyncSession,
    *,
    environment_id: str,
    organization_id: str,
) -> Environment:
    environment = await environments_q.get_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    if environment is None:
        raise NotFound(f"Environment {environment_id} not found")
    return await refresh_build(db, environment)


async def list_environments(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    found = await environments_q.list_environments(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    for environment in found.items:
        await refresh_build(db, environment)
    return found


async def update_environment(
    db: AsyncSession,
    *,
    environment_id: str,
    organization_id: str,
    name: str | None = None,
    description: str | None = None,
    config: dict[str, Any] | None = None,
) -> Environment:
    """Change an environment, rebuilding its image only if the packages moved.

    Renaming should not cost a rebuild, and a rebuild never disturbs a running
    session: those containers already exist, and nothing re-reads the image.
    """
    environment = await get_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    if environment.archived_at is not None:
        raise Conflict("Archived environments are read-only")

    if name is not None and name != environment.name:
        clash = await environments_q.get_environment_by_name(
            db, name=name, organization_id=organization_id
        )
        if clash is not None:
            raise Conflict(f"An environment named {name!r} already exists")

    # The recipe is the packages and the machine they run on — change either and
    # the image no longer matches what was asked for.
    recipe_changed = config is not None and _recipe(config) != _recipe(environment.config)
    await environments_q.update_environment(
        db, environment, name=name, description=description, config=config
    )
    if recipe_changed:
        if _packages(environment.config):
            await _start_build(db, environment, environment.config)
        else:
            # Nothing left to install, so back to the base image. The build id
            # goes too, or a later read would chase a build that no longer
            # describes this environment.
            await environments_q.set_build(
                db, environment, state=READY, image_id=Image.base().image_id, build_id=""
            )
    await db.commit()
    return environment


async def archive_environment(
    db: AsyncSession,
    *,
    environment_id: str,
    organization_id: str,
) -> Environment:
    """Retire an environment. Running sessions keep going; new ones cannot use it."""
    environment = await get_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    await environments_q.archive_environment(db, environment)
    await db.commit()
    return environment


async def delete_environment(
    db: AsyncSession,
    *,
    environment_id: str,
    organization_id: str,
) -> Environment:
    environment = await get_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    in_use = await environments_q.count_sessions_using(db, environment_id=environment_id)
    if in_use:
        raise Conflict(
            f"{in_use} session(s) still use this environment. Archive it instead."
        )
    await environments_q.delete_environment(db, environment)
    await db.commit()
    return environment


async def require_usable(db: AsyncSession, environment: Environment) -> Environment:
    """Check an environment can back a new session, refreshing the build first."""
    if environment.archived_at is not None:
        raise Conflict(f"Environment {environment.id} is archived")
    environment = await refresh_build(db, environment)
    if environment.build_state == BUILDING:
        raise Conflict(f"Environment {environment.id} is still being built")
    if environment.build_state == FAILED:
        raise Conflict(
            f"Environment {environment.id} failed to build: {environment.build_error}"
        )
    return environment


async def refresh_build(db: AsyncSession, environment: Environment) -> Environment:
    """Ask the provider how the build went, if it was still going.

    Done on read rather than by a background loop: nothing calls us when a
    build finishes, and an environment nobody looks at does not need chasing.
    """
    if environment.build_state != BUILDING:
        return environment
    image = Image.from_environment(environment)
    if image is None or image.build_id is None:
        return environment

    status = await image.refresh(db, environment)
    if status.state != BUILDING:
        await db.commit()
    return environment


async def _start_build(
    db: AsyncSession,
    environment: Environment,
    config: dict[str, Any],
) -> None:
    packages, cpu, memory_mb = _recipe(config)
    await Image.build(db, environment, packages=packages, cpu=cpu, memory_mb=memory_mb)


def _recipe(config: dict[str, Any]) -> tuple[dict[str, list[str]], int, int]:
    """Everything that ends up baked into the image, and nothing that does not.

    Comparing two of these is how an edit decides whether to rebuild, so a
    rename or a new description must not show up in here.
    """
    parsed = EnvironmentConfig.model_validate(config or {})
    return _packages(config), parsed.cpu, parsed.memory_mb


def _packages(config: dict[str, Any]) -> dict[str, list[str]]:
    declared = (config or {}).get("packages") or {}
    return {manager: entries for manager, entries in declared.items() if entries}

from fastapi import APIRouter, status

from app.db.models import Environment
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.environments import (
    EnvironmentConfig,
    EnvironmentCreateRequest,
    EnvironmentResponse,
    EnvironmentUpdateRequest,
)
from app.routers.deps import Db, OrganizationId
from app.services import environments as service

router = APIRouter(prefix="/v1/environments", tags=["environments"])


@router.post("", response_model=EnvironmentResponse, status_code=status.HTTP_201_CREATED)
async def create_environment(
    body: EnvironmentCreateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Register an environment.

    Declaring packages starts an image build that takes minutes, so this comes
    back straight away with `build_state: "building"`. Poll this environment
    until it reads `ready` — sessions are refused until then.
    """
    environment = await service.create_environment(
        db,
        organization_id=organization_id,
        name=body.name,
        description=body.description,
        config=body.config.model_dump(),
    )
    return to_environment(environment)


@router.get("", response_model=ListResponse[EnvironmentResponse])
async def list_environments(
    db: Db,
    organization_id: OrganizationId,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    found = await service.list_environments(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return page_of(found, to_environment)


@router.get("/{environment_id}", response_model=EnvironmentResponse)
async def retrieve_environment(environment_id: str, db: Db, organization_id: OrganizationId):
    environment = await service.get_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    return to_environment(environment)


@router.post("/{environment_id}", response_model=EnvironmentResponse)
async def update_environment(
    environment_id: str,
    body: EnvironmentUpdateRequest,
    db: Db,
    organization_id: OrganizationId,
):
    """Changes reach new containers only — running sessions keep the image they started on."""
    environment = await service.update_environment(
        db,
        environment_id=environment_id,
        organization_id=organization_id,
        name=body.name,
        description=body.description,
        config=body.config.model_dump() if body.config is not None else None,
    )
    return to_environment(environment)


@router.post("/{environment_id}/archive", response_model=EnvironmentResponse)
async def archive_environment(environment_id: str, db: Db, organization_id: OrganizationId):
    """Retire it. Running sessions continue; new ones cannot reference it."""
    environment = await service.archive_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    return to_environment(environment)


@router.delete("/{environment_id}", response_model=DeletedResponse)
async def delete_environment(environment_id: str, db: Db, organization_id: OrganizationId):
    """Refused while any session still references it — archive those instead."""
    environment = await service.delete_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    return DeletedResponse(id=environment.id)


def to_environment(environment: Environment) -> EnvironmentResponse:
    """`image_id` and `build_id` stay internal — which image backs an
    environment is our business, not part of the contract."""
    return EnvironmentResponse(
        id=environment.id,
        name=environment.name,
        description=environment.description,
        config=EnvironmentConfig.model_validate(environment.config or {}),
        build_state=environment.build_state,
        build_error=environment.build_error,
        created_at=environment.created_at,
        updated_at=environment.updated_at,
        archived_at=environment.archived_at,
    )

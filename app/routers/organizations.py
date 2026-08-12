from fastapi import APIRouter

router = APIRouter(
    prefix="/v1/organizations",
    tags=["organizations"],
    include_in_schema=False,
)


@router.post("")
async def create_organization(): ...


@router.get("")
async def list_organizations(): ...


@router.get("/{organization_id}")
async def retrieve_organization(organization_id: str): ...


@router.post("/{organization_id}")
async def update_organization(organization_id: str): ...


@router.post("/{organization_id}/archive")
async def archive_organization(organization_id: str): ...


@router.get("/{organization_id}/members")
async def list_members(organization_id: str): ...


@router.post("/{organization_id}/members")
async def add_member(organization_id: str): ...


@router.delete("/{organization_id}/members/{user_id}")
async def remove_member(organization_id: str, user_id: str): ...

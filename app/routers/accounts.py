from fastapi import APIRouter

router = APIRouter(prefix="/v1/organizations", tags=["organizations"])


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


@router.get("/{organization_id}/owners")
async def list_owners(organization_id: str): ...


@router.post("/{organization_id}/owners")
async def add_owner(organization_id: str): ...


@router.delete("/{organization_id}/owners/{user_id}")
async def remove_owner(organization_id: str, user_id: str): ...

from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_api_access
from app.models.common import ListResponse
from app.models.model_providers import ModelProviderResponse, model_provider_to_response
from app.runtime.providers import (
    retrieve_runtime_provider_catalog_entry,
    runtime_provider_catalog,
)

router = APIRouter(
    prefix="/v1/model_providers",
    tags=["model_providers"],
    dependencies=[Depends(require_api_access)],
)


@router.get("", response_model=ListResponse[ModelProviderResponse])
async def list_model_providers() -> ListResponse[ModelProviderResponse]:
    return ListResponse.from_items(
        [model_provider_to_response(entry) for entry in runtime_provider_catalog()]
    )


@router.get("/{provider_id}", response_model=ModelProviderResponse)
async def retrieve_model_provider(provider_id: str) -> ModelProviderResponse:
    entry = retrieve_runtime_provider_catalog_entry(provider_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Model provider not found")
    return model_provider_to_response(entry)

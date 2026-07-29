from fastapi import APIRouter

router = APIRouter(prefix="/v1/models", tags=["models"])


@router.get("")
async def list_models(): ...


@router.get("/{model_id}")
async def retrieve_model(model_id: str): ...

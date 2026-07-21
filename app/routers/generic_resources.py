import hashlib
import json
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_access
from app.db.engine import get_session
from app.db.queries import agents as agents_q
from app.db.queries import environments as env_q
from app.db.queries import events as events_q
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.event_validation import validate_system_message_batch, validate_user_define_outcome_event
from app.metadata import merge_metadata, normalize_metadata
from app.models.common import ListResponse, utcnow
from app.secret_cipher import encrypt_secret_values, is_secret_key
from app.models.memory_stores import (
    MemoryCreateRequest,
    MemoryDeletedResponse,
    MemoryPrefixResponse,
    MemoryResponse,
    MemoryStoreCreateRequest,
    MemoryStoreDeletedResponse,
    MemoryStoreResponse,
    MemoryStoreUpdateRequest,
    MemoryUpdateRequest,
    MemoryVersionResponse,
)
from app.models.model_providers import (
    ModelCredentialCreateRequest,
    ModelCredentialDeletedResponse,
    ModelCredentialResponse,
    ModelCredentialRotateRequest,
)
from app.models.resources import GenericBody, deleted_response, resource_to_response
from app.models.vaults import (
    VaultCreateRequest,
    VaultDeletedResponse,
    VaultResponse,
    VaultUpdateRequest,
)
from app.pagination import (
    decode_page_offset,
    encode_page_offset,
    filter_created_at,
    normalize_page_limit,
    normalize_sort_order,
    paginate,
    sort_by_created_at,
)
from app.runtime.agent_resolution import resolve_session_agent_config
from app.runtime.model_credentials import (
    MODEL_CREDENTIAL_KIND,
    ModelCredentialRequiredError,
)
from app.runtime.providers import (
    registered_runtime_provider_api_key_env,
    retrieve_runtime_provider_catalog_entry,
)
from app.runtime.sandbox_lifecycle import (
    SandboxLifecycleConfigurationError,
    provision_session_sandbox,
)
from app.runtime.sandbox_providers import SandboxProviderError
from app.session_resources import (
    create_session_resource,
    validate_user_message_file_references,
)

router = APIRouter(tags=["managed resources"], dependencies=[Depends(require_api_access)])


RESOURCE_CONFIG = {
    "vaults": ("vault", "vault"),
    "memory_stores": ("memory_store", "memory_store"),
    "deployments": ("deployment", "deployment"),
    "deployment_runs": ("deployment_run", "deployment_run"),
    "user_profiles": ("user_profile", "user_profile"),
}

MAX_MEMORIES_PER_STORE = 2000
MAX_MEMORY_CONTENT_BYTES = 100 * 1024
MAX_MEMORY_PATH_BYTES = 1024
DEPLOYMENT_RUN_TRIGGER_TYPES = {"manual", "schedule"}
MEMORY_VIEWS = {"basic", "full"}
MEMORY_VERSION_OPERATIONS = {"created", "deleted", "modified"}
USER_PROFILE_RELATIONSHIPS = {"external", "internal", "resold"}
MAX_USER_PROFILE_FIELD_CHARS = 255
MAX_DISPLAY_NAME_CHARS = 255
MAX_MEMORY_STORE_DESCRIPTION_CHARS = 1024
MAX_DEPLOYMENT_RESOURCES = 500
MAX_DEPLOYMENT_VAULT_IDS = 50
MAX_CREDENTIALS_PER_VAULT = 20
CREDENTIAL_AUTH_TYPES = {"environment_variable", "mcp_oauth", "static_bearer"}
CREDENTIAL_TOKEN_ENDPOINT_AUTH_TYPES = {"client_secret_basic", "client_secret_post", "none"}
RESERVED_MODEL_CREDENTIAL_FIELDS = {"credential_kind", "model_provider"}


@router.post("/v1/vaults", response_model=VaultResponse, status_code=201)
async def create_vault(
    body: VaultCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    return await _create_top_level(
        db,
        "vault",
        body.model_dump(mode="json", exclude_unset=True),
    )


@router.get("/v1/vaults", response_model=ListResponse[VaultResponse])
async def list_vaults(
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_session),
):
    return await _list_top_level(db, "vault", limit, page=page, include_archived=include_archived, max_limit=100)


@router.get("/v1/vaults/{vault_id}", response_model=VaultResponse)
async def retrieve_vault(vault_id: str, db: AsyncSession = Depends(get_session)):
    return await _retrieve(db, vault_id, "vault")


@router.post("/v1/vaults/{vault_id}", response_model=VaultResponse)
async def update_vault(
    vault_id: str,
    body: VaultUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    return await _update(
        db,
        vault_id,
        "vault",
        body.model_dump(mode="json", exclude_unset=True),
    )


@router.delete("/v1/vaults/{vault_id}", response_model=VaultDeletedResponse)
async def delete_vault(vault_id: str, db: AsyncSession = Depends(get_session)):
    return await _delete(db, vault_id, "vault", "vault_deleted")


@router.post("/v1/vaults/{vault_id}/archive", response_model=VaultResponse)
async def archive_vault(vault_id: str, db: AsyncSession = Depends(get_session)):
    return await _archive(db, vault_id, "vault")


@router.post("/v1/vaults/{vault_id}/credentials", status_code=201)
async def create_credential(vault_id: str, body: GenericBody, db: AsyncSession = Depends(get_session)):
    raw_data = body.model_dump(mode="json")
    _reject_reserved_model_credential_fields(raw_data)
    resource = await _create_credential_resource(
        db,
        vault_id=vault_id,
        raw_data=raw_data,
    )
    return _credential_response(resource)


@router.post(
    "/v1/vaults/{vault_id}/model_credentials",
    response_model=ModelCredentialResponse,
    status_code=201,
)
async def create_model_credential(
    vault_id: str,
    body: ModelCredentialCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> ModelCredentialResponse:
    """Create provider BYOK without exposing its internal secret-name mapping."""

    provider = retrieve_runtime_provider_catalog_entry(body.provider)
    if provider is None:
        raise HTTPException(status_code=404, detail="Model provider not found")
    secret_name = registered_runtime_provider_api_key_env(provider.id)
    if provider.credential_type != "api_key" or secret_name is None:
        raise HTTPException(
            status_code=422,
            detail=f"Model provider {provider.id} does not accept API-key credentials",
        )

    resource = await _create_credential_resource(
        db,
        vault_id=vault_id,
        duplicate_detail=(
            f"An active model credential already exists for {provider.id}"
        ),
        raw_data={
            "display_name": body.display_name or f"{provider.display_name} API key",
            "metadata": body.metadata,
            "model_provider": provider.id,
            "credential_kind": MODEL_CREDENTIAL_KIND,
            "auth": {
                "type": "environment_variable",
                "secret_name": secret_name,
                "secret_value": body.api_key.get_secret_value(),
                "networking": {"type": "unrestricted"},
            },
        },
    )
    return _model_credential_response(resource)


@router.get(
    "/v1/vaults/{vault_id}/model_credentials",
    response_model=ListResponse[ModelCredentialResponse],
)
async def list_model_credentials(
    vault_id: str,
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_session),
) -> ListResponse[ModelCredentialResponse]:
    """List only native provider BYOK Credentials in a Vault."""

    vault = await _must_exist(db, vault_id, "vault")
    credentials = await res_q.list_resources(
        db,
        resource_type="credential",
        parent_id=vault_id,
        limit=MAX_CREDENTIALS_PER_VAULT + 1,
        include_archived=include_archived,
        organization_id=vault.organization_id,
    )
    model_credentials = [
        credential
        for credential in credentials
        if _model_credential_provider_id(credential) is not None
    ]
    model_credentials = sort_by_created_at(model_credentials, order="desc")
    return paginate(
        [_model_credential_response(credential) for credential in model_credentials],
        limit=limit,
        page=page,
        max_limit=MAX_CREDENTIALS_PER_VAULT,
    )


@router.get(
    "/v1/vaults/{vault_id}/model_credentials/{credential_id}",
    response_model=ModelCredentialResponse,
)
async def retrieve_model_credential(
    vault_id: str,
    credential_id: str,
    db: AsyncSession = Depends(get_session),
) -> ModelCredentialResponse:
    credential = await _must_exist(
        db,
        credential_id,
        "credential",
        parent_id=vault_id,
    )
    return _model_credential_response(credential)


@router.post(
    "/v1/vaults/{vault_id}/model_credentials/{credential_id}",
    response_model=ModelCredentialResponse,
)
async def rotate_model_credential(
    vault_id: str,
    credential_id: str,
    body: ModelCredentialRotateRequest,
    db: AsyncSession = Depends(get_session),
) -> ModelCredentialResponse:
    """Rotate provider BYOK without exposing its internal credential shape."""

    vault, credential = await _lock_vault_and_credential(db, vault_id, credential_id)
    provider_id = _model_credential_provider_id(credential)
    if provider_id is None:
        raise HTTPException(status_code=404, detail="Model credential not found")
    provider = retrieve_runtime_provider_catalog_entry(provider_id)
    expected_secret_name = registered_runtime_provider_api_key_env(provider_id)
    auth = (credential.data or {}).get("auth") or {}
    if (
        provider is None
        or provider.credential_type != "api_key"
        or expected_secret_name is None
        or auth.get("type") != "environment_variable"
        or auth.get("secret_name") != expected_secret_name
    ):
        raise HTTPException(status_code=404, detail="Model credential not found")
    if vault.archived_at is not None:
        raise HTTPException(status_code=409, detail="Vault is archived")
    if credential.archived_at is not None:
        raise HTTPException(status_code=409, detail="Model credential is archived")

    await _update_resource(
        db,
        credential,
        "credential",
        {
            "auth": {
                "type": "environment_variable",
                "secret_value": body.api_key.get_secret_value(),
            }
        },
    )
    return _model_credential_response(credential)


@router.post(
    "/v1/vaults/{vault_id}/model_credentials/{credential_id}/archive",
    response_model=ModelCredentialResponse,
)
async def archive_model_credential(
    vault_id: str,
    credential_id: str,
    db: AsyncSession = Depends(get_session),
) -> ModelCredentialResponse:
    _, credential = await _lock_vault_and_credential(db, vault_id, credential_id)
    if _model_credential_provider_id(credential) is None:
        raise HTTPException(status_code=404, detail="Model credential not found")
    await res_q.update_resource(
        db,
        credential,
        data=_purge_credential_secret_data(credential.data),
    )
    await res_q.archive_resource(db, credential)
    await db.commit()
    return _model_credential_response(credential)


@router.delete(
    "/v1/vaults/{vault_id}/model_credentials/{credential_id}",
    response_model=ModelCredentialDeletedResponse,
)
async def delete_model_credential(
    vault_id: str,
    credential_id: str,
    db: AsyncSession = Depends(get_session),
) -> ModelCredentialDeletedResponse:
    _, credential = await _lock_vault_and_credential(db, vault_id, credential_id)
    if _model_credential_provider_id(credential) is None:
        raise HTTPException(status_code=404, detail="Model credential not found")
    await res_q.update_resource(
        db,
        credential,
        data=_purge_credential_secret_data(credential.data),
    )
    await res_q.delete_resource(db, credential)
    await db.commit()
    return ModelCredentialDeletedResponse(id=credential.id)


async def _create_credential_resource(
    db: AsyncSession,
    *,
    vault_id: str,
    raw_data: dict[str, Any],
    duplicate_detail: str | None = None,
):
    vault = await _must_exist(db, vault_id, "vault", for_update=True)
    if vault.archived_at is not None:
        raise HTTPException(status_code=409, detail="Vault is archived")

    data = _normalize_credential_data(raw_data)
    _require_credential_create_secret(data["auth"])
    credentials = await res_q.list_resources(
        db,
        resource_type="credential",
        parent_id=vault_id,
        limit=MAX_CREDENTIALS_PER_VAULT + 1,
        include_archived=False,
        organization_id=vault.organization_id,
    )
    if len(credentials) >= MAX_CREDENTIALS_PER_VAULT:
        raise HTTPException(status_code=400, detail="A vault supports at most 20 active credentials")
    key = _credential_unique_key(data["auth"])
    if any(_credential_unique_key((credential.data or {}).get("auth") or {}) == key for credential in credentials):
        raise HTTPException(
            status_code=409,
            detail=duplicate_detail
            or f"An active credential already exists for {key[1]}",
        )

    resource = await res_q.create_resource(
        db,
        resource_type="credential",
        parent_id=vault_id,
        name=data.get("name") or data.get("display_name"),
        data=data,
        organization_id=vault.organization_id,
    )
    await db.commit()
    return resource


@router.get("/v1/vaults/{vault_id}/credentials")
async def list_credentials(
    vault_id: str,
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_session),
):
    vault = await _must_exist(db, vault_id, "vault")
    credentials = await res_q.list_resources(
        db,
        resource_type="credential",
        parent_id=vault_id,
        limit=MAX_CREDENTIALS_PER_VAULT + 1,
        include_archived=include_archived,
        organization_id=vault.organization_id,
    )
    generic_credentials = [
        credential
        for credential in credentials
        if _model_credential_provider_id(credential) is None
    ]
    generic_credentials = sort_by_created_at(generic_credentials, order="desc")
    return paginate(
        [_credential_response(credential) for credential in generic_credentials],
        limit=limit,
        page=page,
        max_limit=MAX_CREDENTIALS_PER_VAULT,
    )


@router.get("/v1/vaults/{vault_id}/credentials/{credential_id}")
async def retrieve_credential(vault_id: str, credential_id: str, db: AsyncSession = Depends(get_session)):
    credential = await _must_exist(db, credential_id, "credential", parent_id=vault_id)
    _require_generic_credential(credential)
    return _credential_response(credential)


@router.post("/v1/vaults/{vault_id}/credentials/{credential_id}")
async def update_credential(
    vault_id: str,
    credential_id: str,
    body: GenericBody,
    db: AsyncSession = Depends(get_session),
):
    vault, credential = await _lock_vault_and_credential(db, vault_id, credential_id)
    _require_generic_credential(credential)
    if vault.archived_at is not None:
        raise HTTPException(status_code=409, detail="Vault is archived")
    if credential.archived_at is not None:
        raise HTTPException(status_code=409, detail="Credential is archived")
    raw_data = body.model_dump(mode="json")
    _reject_reserved_model_credential_fields(raw_data)
    return await _update_resource(
        db,
        credential,
        "credential",
        raw_data,
    )


@router.delete("/v1/vaults/{vault_id}/credentials/{credential_id}")
async def delete_credential(vault_id: str, credential_id: str, db: AsyncSession = Depends(get_session)):
    return await _delete(db, credential_id, "credential", "vault_credential_deleted", parent_id=vault_id)


@router.post("/v1/vaults/{vault_id}/credentials/{credential_id}/archive")
async def archive_credential(vault_id: str, credential_id: str, db: AsyncSession = Depends(get_session)):
    return await _archive(db, credential_id, "credential", parent_id=vault_id)


@router.post("/v1/vaults/{vault_id}/credentials/{credential_id}/mcp_oauth_validate")
async def validate_credential(vault_id: str, credential_id: str, db: AsyncSession = Depends(get_session)):
    vault, credential = await _lock_vault_and_credential(db, vault_id, credential_id)
    _require_generic_credential(credential)
    if vault.archived_at is not None:
        raise HTTPException(status_code=409, detail="Vault is archived")
    if credential.archived_at is not None:
        raise HTTPException(status_code=409, detail="Credential is archived")
    validation = _credential_validation_payload(credential, vault_id=vault_id)
    data = dict(credential.data or {})
    metadata = dict(data.get("metadata") or {})
    last_validation = {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in validation.items()
    }
    metadata["last_validation"] = json.dumps(last_validation, separators=(",", ":"), sort_keys=True)
    data["metadata"] = metadata
    await res_q.update_resource(db, credential, data=data)
    await db.commit()
    return validation


def _credential_validation_payload(credential, *, vault_id: str) -> dict[str, Any]:
    auth = (credential.data or {}).get("auth") or {}
    return {
        "type": "vault_credential_validation",
        "vault_id": vault_id,
        "credential_id": credential.id,
        "status": "unknown",
        "has_refresh_token": bool(auth.get("refresh")),
        "mcp_probe": None,
        "refresh": None,
        "validated_at": utcnow(),
    }


@router.post(
    "/v1/memory_stores",
    response_model=MemoryStoreResponse,
    status_code=201,
)
async def create_memory_store(body: MemoryStoreCreateRequest, db: AsyncSession = Depends(get_session)):
    return await _create_top_level(db, "memory_store", body.model_dump(mode="json"))


@router.get(
    "/v1/memory_stores",
    response_model=ListResponse[MemoryStoreResponse],
)
async def list_memory_stores(
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    db: AsyncSession = Depends(get_session),
):
    return await _list_top_level(
        db,
        "memory_store",
        limit,
        page=page,
        include_archived=include_archived,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        max_limit=100,
    )


@router.get(
    "/v1/memory_stores/{memory_store_id}",
    response_model=MemoryStoreResponse,
)
async def retrieve_memory_store(memory_store_id: str, db: AsyncSession = Depends(get_session)):
    return await _retrieve(db, memory_store_id, "memory_store")


@router.post(
    "/v1/memory_stores/{memory_store_id}",
    response_model=MemoryStoreResponse,
)
async def update_memory_store(
    memory_store_id: str,
    body: MemoryStoreUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    update = body.model_dump(mode="json", exclude_unset=True)
    if update.get("name") is None:
        update.pop("name", None)
    return await _update(
        db,
        memory_store_id,
        "memory_store",
        update,
    )


@router.delete(
    "/v1/memory_stores/{memory_store_id}",
    response_model=MemoryStoreDeletedResponse,
)
async def delete_memory_store(memory_store_id: str, db: AsyncSession = Depends(get_session)):
    return await _delete(db, memory_store_id, "memory_store", "memory_store_deleted")


@router.post(
    "/v1/memory_stores/{memory_store_id}/archive",
    response_model=MemoryStoreResponse,
)
async def archive_memory_store(memory_store_id: str, db: AsyncSession = Depends(get_session)):
    return await _archive(db, memory_store_id, "memory_store")


@router.post(
    "/v1/memory_stores/{memory_store_id}/memories",
    response_model=MemoryResponse,
    status_code=201,
)
async def create_memory(
    memory_store_id: str,
    body: MemoryCreateRequest,
    view: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    await _must_write_memory_store(db, memory_store_id)
    await _ensure_memory_store_capacity(db, memory_store_id)
    data = _memory_payload(body.model_dump(mode="json", exclude_unset=True))
    existing = await _find_memory_by_path(db, memory_store_id, data["path_key"])
    if existing is not None:
        raise HTTPException(status_code=409, detail="Memory path already exists in this memory store")
    memory = await res_q.create_resource(
        db,
        resource_type="memory",
        parent_id=memory_store_id,
        name=data["path_key"],
        data=data,
    )
    version = await _create_memory_version(db, memory, version=1, actor=data["updated_by"], operation="created")
    data["memory_version_id"] = version.id
    await res_q.update_resource(db, memory, data=data, name=data["path_key"])
    await db.commit()
    return _resource_response(memory, view=view)


@router.get(
    "/v1/memory_stores/{memory_store_id}/memories",
    response_model=ListResponse[MemoryResponse | MemoryPrefixResponse],
)
async def list_memories(
    memory_store_id: str,
    limit: int = 50,
    page: str | None = None,
    path: str | None = None,
    path_prefix: str | None = None,
    depth: int | None = None,
    view: str | None = None,
    order: str = "asc",
    order_by: str = "path",
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    await _must_exist(db, memory_store_id, "memory_store")
    if path is not None:
        path_key = _path_key(_normalize_memory_path(path))
        memory = await _find_memory_by_path(db, memory_store_id, path_key)
        resources = [memory] if memory is not None else []
    elif path_prefix is not None:
        prefix = _path_key(_normalize_memory_path_prefix(path_prefix))
        if prefix:
            resources = await res_q.list_resources_by_name_prefix(
                db,
                resource_type="memory",
                parent_id=memory_store_id,
                name_prefix=prefix,
                limit=MAX_MEMORIES_PER_STORE,
            )
        else:
            resources = await res_q.list_resources(
                db,
                resource_type="memory",
                parent_id=memory_store_id,
                limit=MAX_MEMORIES_PER_STORE,
            )
    else:
        resources = await res_q.list_resources(
            db,
            resource_type="memory",
            parent_id=memory_store_id,
            limit=MAX_MEMORIES_PER_STORE,
        )
    if path is not None and path_prefix is not None:
        prefix = _path_key(_normalize_memory_path_prefix(path_prefix))
        if prefix:
            resources = [
                memory
                for memory in resources
                if memory.name == prefix or str(memory.name or "").startswith(f"{prefix}/")
            ]
    resources = _sort_memories(resources, order=order, order_by=order_by)
    if depth is not None and path is None:
        return paginate(
            _memory_list_items_with_depth(resources, view=view, depth=depth, path_prefix=path_prefix, order=order),
            limit=limit,
            page=page,
        )
    return paginate([_resource_response(memory, view=view) for memory in resources], limit=limit, page=page)


@router.get(
    "/v1/memory_stores/{memory_store_id}/memories/by_path",
    response_model=MemoryResponse,
)
async def retrieve_memory_by_path(
    memory_store_id: str,
    path: str = Query(...),
    view: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    await _must_exist(db, memory_store_id, "memory_store")
    memory = await _find_memory_by_path(db, memory_store_id, _path_key(_normalize_memory_path(path)))
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _resource_response(memory, view=view)


@router.get(
    "/v1/memory_stores/{memory_store_id}/memories/{memory_id}",
    response_model=MemoryResponse,
)
async def retrieve_memory(
    memory_store_id: str,
    memory_id: str,
    view: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    memory = await _must_exist(db, memory_id, "memory", parent_id=memory_store_id)
    return _resource_response(memory, view=view)


@router.post(
    "/v1/memory_stores/{memory_store_id}/memories/{memory_id}",
    response_model=MemoryResponse,
)
async def update_memory(
    memory_store_id: str,
    memory_id: str,
    body: MemoryUpdateRequest,
    view: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    await _must_write_memory_store(db, memory_store_id)
    memory = await _must_exist(db, memory_id, "memory", parent_id=memory_store_id)
    update = body.model_dump(mode="json", exclude_unset=True)
    if update.get("path") is None:
        update.pop("path", None)
    expected_version = update.pop("if_version", update.pop("expected_version", None))
    precondition = update.pop("precondition", None)
    current_version = int(memory.data.get("version") or 1)
    if expected_version is not None and int(expected_version) != current_version:
        raise HTTPException(status_code=409, detail="Memory version precondition failed")
    if isinstance(precondition, dict) and precondition.get("type") == "content_sha256":
        expected_sha = precondition.get("content_sha256")
        if expected_sha and expected_sha != memory.data.get("content_sha256"):
            if _memory_requested_state_matches_current(memory.data, update):
                return _resource_response(memory, view=view)
            raise HTTPException(status_code=409, detail="Memory content precondition failed")
    data = _merge_memory_data(memory.data, update)
    if not _memory_has_material_change(memory.data, data):
        return _resource_response(memory, view=view)
    if data["path_key"] != memory.data.get("path_key"):
        existing = await _find_memory_by_path(db, memory_store_id, data["path_key"])
        if existing is not None and existing.id != memory.id:
            raise HTTPException(status_code=409, detail="Memory path already exists in this memory store")
    await res_q.update_resource(db, memory, data=data, name=data["path_key"])
    version = int(data["version"])
    version_resource = await _create_memory_version(
        db,
        memory,
        version=version,
        actor=data["updated_by"],
        operation="modified",
    )
    data["memory_version_id"] = version_resource.id
    await res_q.update_resource(db, memory, data=data, name=data["path_key"])
    await db.commit()
    return _resource_response(memory, view=view)


@router.delete(
    "/v1/memory_stores/{memory_store_id}/memories/{memory_id}",
    response_model=MemoryDeletedResponse,
)
async def delete_memory(
    memory_store_id: str,
    memory_id: str,
    expected_content_sha256: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    await _must_write_memory_store(db, memory_store_id)
    memory = await _must_exist(db, memory_id, "memory", parent_id=memory_store_id)
    if expected_content_sha256 is not None and expected_content_sha256 != memory.data.get("content_sha256"):
        raise HTTPException(status_code=409, detail="Memory content precondition failed")
    data = dict(memory.data or {})
    data["version"] = int(data.get("version") or 1) + 1
    data["updated_by"] = str(data.get("updated_by") or "api")
    data["updated_at"] = utcnow().isoformat()
    version_resource = await _create_memory_version(
        db,
        memory,
        version=int(data["version"]),
        actor=data["updated_by"],
        operation="deleted",
        data=data,
    )
    data["memory_version_id"] = version_resource.id
    await res_q.update_resource(db, memory, data=data)
    await res_q.delete_resource(db, memory)
    await db.commit()
    return deleted_response(memory, public_type="memory_deleted")


@router.get(
    "/v1/memory_stores/{memory_store_id}/memories/{memory_id}/versions",
    response_model=ListResponse[MemoryVersionResponse],
)
async def list_memory_versions_for_memory(
    memory_store_id: str,
    memory_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    await _must_exist(db, memory_id, "memory", parent_id=memory_store_id)
    page_size = normalize_page_limit(limit)
    offset = decode_page_offset(page)
    versions = await res_q.list_memory_versions_page(
        db,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        limit=page_size + 1,
        offset=offset,
    )
    return _memory_version_page(versions, view=None, page_size=page_size, offset=offset)


@router.get(
    "/v1/memory_stores/{memory_store_id}/memories/{memory_id}/versions/{version}",
    response_model=MemoryVersionResponse,
)
async def retrieve_memory_version_for_memory(
    memory_store_id: str,
    memory_id: str,
    version: int,
    db: AsyncSession = Depends(get_session),
):
    await _must_exist(db, memory_id, "memory", parent_id=memory_store_id)
    memory_version = await res_q.get_resource_version(
        db,
        resource_type="memory_version",
        parent_id=memory_id,
        version=version,
    )
    if memory_version is None:
        raise HTTPException(status_code=404, detail="Memory version not found")
    return _resource_response(memory_version)


@router.get(
    "/v1/memory_stores/{memory_store_id}/memory_versions",
    response_model=ListResponse[MemoryVersionResponse],
)
async def list_memory_versions(
    memory_store_id: str,
    limit: int = 50,
    page: str | None = None,
    memory_id: str | None = None,
    operation: str | None = None,
    api_key_id: str | None = None,
    session_id: str | None = None,
    view: str | None = None,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    if operation is not None and operation not in MEMORY_VERSION_OPERATIONS:
        raise HTTPException(status_code=422, detail="operation must be created, deleted, or modified")
    await _must_exist(db, memory_store_id, "memory_store")
    page_size = normalize_page_limit(limit)
    offset = decode_page_offset(page)
    versions = await res_q.list_memory_versions_page(
        db,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        operation=operation,
        api_key_id=api_key_id,
        session_id=session_id,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        limit=page_size + 1,
        offset=offset,
    )
    return _memory_version_page(versions, view=view, page_size=page_size, offset=offset)


@router.get(
    "/v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}",
    response_model=MemoryVersionResponse,
)
async def retrieve_memory_version(
    memory_store_id: str,
    memory_version_id: str,
    view: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    view = _normalize_memory_view(view)
    await _must_exist(db, memory_store_id, "memory_store")
    version = await res_q.get_resource(db, resource_id=memory_version_id, resource_type="memory_version")
    if version is None:
        raise HTTPException(status_code=404, detail="Memory version not found")
    memory = await res_q.get_resource(
        db,
        resource_id=version.parent_id,
        resource_type="memory",
        parent_id=memory_store_id,
        include_deleted=True,
    )
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory version not found")
    return _resource_response(version, view=view)


@router.post(
    "/v1/memory_stores/{memory_store_id}/memory_versions/{memory_version_id}/redact",
    response_model=MemoryVersionResponse,
)
async def redact_memory_version(
    memory_store_id: str,
    memory_version_id: str,
    db: AsyncSession = Depends(get_session),
):
    await _must_exist(db, memory_store_id, "memory_store")
    version = await res_q.get_resource(db, resource_id=memory_version_id, resource_type="memory_version")
    if version is None:
        raise HTTPException(status_code=404, detail="Memory version not found")
    memory = await res_q.get_resource(
        db,
        resource_id=version.parent_id,
        resource_type="memory",
        parent_id=memory_store_id,
        include_deleted=True,
    )
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory version not found")
    data = dict(version.data)
    if memory.deleted_at is None and (
        memory.data.get("memory_version_id") == version.id
        or int(memory.data.get("version") or 0) == int(version.version or data.get("memory_version") or 0)
    ):
        raise HTTPException(status_code=409, detail="Current live memory version cannot be redacted")
    snapshot = dict(data.get("snapshot") or {})
    for field in (
        "content",
        "path",
        "path_key",
        "content_sha256",
        "content_size_bytes",
    ):
        data.pop(field, None)
        snapshot.pop(field, None)
    snapshot["redacted"] = True
    data["snapshot"] = snapshot
    data["redacted"] = True
    data["redacted_at"] = utcnow().isoformat()
    await res_q.update_resource(db, version, data=data)
    if memory.deleted_at is None and memory.data.get("version") == data.get("memory_version"):
        memory_data = dict(memory.data)
        memory_data.pop("content", None)
        memory_data["redacted"] = True
        memory_data["redacted_at"] = data["redacted_at"]
        await res_q.update_resource(db, memory, data=memory_data)
    await db.commit()
    return _resource_response(version)


@router.post("/v1/deployments", status_code=201)
async def create_deployment(body: GenericBody, db: AsyncSession = Depends(get_session)):
    data = _normalize_deployment_data(body.model_dump(mode="json"))
    await _validate_deployment_definition(db, data)
    return await _create_top_level(db, "deployment", data, status=data["status"])


@router.get("/v1/deployments")
async def list_deployments(
    limit: int = 50,
    page: str | None = None,
    include_archived: bool = False,
    agent_id: str | None = None,
    status: str | None = None,
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    db: AsyncSession = Depends(get_session),
):
    if status is not None and status not in {"active", "paused"}:
        raise HTTPException(status_code=422, detail="Deployment status filter must be active or paused")
    if include_archived and status is not None:
        raise HTTPException(status_code=422, detail="status cannot be combined with include_archived")
    return await _list_top_level(
        db,
        "deployment",
        limit,
        page=page,
        include_archived=include_archived,
        agent_id=agent_id,
        status=status,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        max_limit=100,
    )


@router.get("/v1/deployments/{deployment_id}")
async def retrieve_deployment(deployment_id: str, db: AsyncSession = Depends(get_session)):
    return await _retrieve(db, deployment_id, "deployment")


@router.post("/v1/deployments/{deployment_id}")
async def update_deployment(deployment_id: str, body: GenericBody, db: AsyncSession = Depends(get_session)):
    deployment = await _must_exist(db, deployment_id, "deployment")
    _ensure_deployment_mutable(deployment)
    data = _normalize_deployment_data(_merge_data(deployment.data, body.model_dump(mode="json")))
    await _validate_deployment_definition(db, data)
    await res_q.update_resource(
        db,
        deployment,
        data=data,
        name=data.get("name") or data.get("display_name") or deployment.name,
        status=data["status"],
    )
    await db.commit()
    return _resource_response(deployment)


@router.post("/v1/deployments/{deployment_id}/archive")
async def archive_deployment(deployment_id: str, db: AsyncSession = Depends(get_session)):
    return await _archive(db, deployment_id, "deployment")


@router.post("/v1/deployments/{deployment_id}/pause")
async def pause_deployment(deployment_id: str, db: AsyncSession = Depends(get_session)):
    deployment = await _must_exist(db, deployment_id, "deployment")
    _ensure_deployment_mutable(deployment)
    data = dict(deployment.data)
    data["status"] = "paused"
    data["paused_reason"] = {"type": "manual"}
    await res_q.update_resource(db, deployment, data=data, status="paused")
    await db.commit()
    return _resource_response(deployment)


@router.post("/v1/deployments/{deployment_id}/unpause")
async def unpause_deployment(deployment_id: str, db: AsyncSession = Depends(get_session)):
    deployment = await _must_exist(db, deployment_id, "deployment")
    _ensure_deployment_mutable(deployment)
    data = dict(deployment.data)
    data["status"] = "active"
    data.pop("paused_reason", None)
    await res_q.update_resource(db, deployment, data=data, status="active")
    await db.commit()
    return _resource_response(deployment)


@router.post("/v1/deployments/{deployment_id}/run")
async def run_deployment(
    deployment_id: str,
    body: GenericBody | None = Body(default=None),
    db: AsyncSession = Depends(get_session),
):
    deployment = await _must_exist(db, deployment_id, "deployment")
    run_input = body.model_dump(mode="json") if body is not None else {}
    run = await _run_deployment_resource(db, deployment, run_input)
    await db.commit()
    return _resource_response(run)


async def run_due_scheduled_deployments(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    now = _parse_datetime(now) or utcnow()
    deployments = await res_q.list_resources(
        db,
        resource_type="deployment",
        limit=1000,
        include_archived=False,
    )
    runs = []
    for deployment in deployments:
        if len(runs) >= limit:
            break
        due_at = await _scheduled_deployment_due_at(db, deployment, now=now)
        if due_at is None:
            continue
        run = await _run_deployment_resource(
            db,
            deployment,
            {"trigger": "schedule", "scheduled_for": due_at.isoformat()},
        )
        runs.append(_resource_response(run))
    return runs


async def _run_deployment_resource(db: AsyncSession, deployment, run_input: dict[str, Any]):
    _ensure_deployment_mutable(deployment)
    trigger = run_input.get("trigger") or run_input.get("trigger_type") or "manual"
    if (deployment.status == "paused" or deployment.data.get("status") == "paused") and trigger != "manual":
        raise HTTPException(status_code=409, detail="Deployment schedule is paused")
    deployment_data = dict(deployment.data or {})
    await _archive_if_deployment_agent_unusable(db, deployment, deployment_data)
    attempt = int(run_input.get("attempt") or 1)
    if trigger == "schedule":
        schedule = dict(deployment_data.get("schedule") or {})
        if schedule.get("expression") and schedule.get("timezone"):
            schedule["last_run_at"] = utcnow().isoformat()
            schedule["upcoming_runs_at"] = _upcoming_cron_runs(str(schedule["expression"]), str(schedule["timezone"]))
            deployment_data["schedule"] = schedule
            await res_q.update_resource(db, deployment, data=deployment_data)
    run = await res_q.create_resource(
        db,
        resource_type="deployment_run",
        parent_id=deployment.id,
        status="queued",
        data={
            "deployment_id": deployment.id,
            "agent": deployment_data.get("agent"),
            "status": "queued",
            "attempt": attempt,
            "trigger": trigger,
            "trigger_context": _trigger_context(run_input),
            "scheduled_for": run_input.get("scheduled_for"),
        },
    )
    session = None
    try:
        session = await _maybe_create_deployment_session(db, deployment, run, run_input)
    except DeploymentRunCreationError as exc:
        run_data = dict(run.data or {})
        run_data["status"] = "error"
        run_data["error"] = exc.error
        await res_q.update_resource(db, run, data=run_data, status="error")
    if session is not None:
        run_data = dict(run.data)
        run_data["session_id"] = session.id
        await res_q.update_resource(db, run, data=run_data)
    return run


@router.get("/v1/deployment_runs")
async def list_deployment_runs(
    limit: int = 50,
    page: str | None = None,
    deployment_id: str | None = None,
    has_error: bool | None = None,
    trigger_type: str | None = None,
    created_at_gt: datetime | None = Query(default=None, alias="created_at[gt]"),
    created_at_gte: datetime | None = Query(default=None, alias="created_at[gte]"),
    created_at_lt: datetime | None = Query(default=None, alias="created_at[lt]"),
    created_at_lte: datetime | None = Query(default=None, alias="created_at[lte]"),
    db: AsyncSession = Depends(get_session),
):
    if trigger_type is not None and trigger_type not in DEPLOYMENT_RUN_TRIGGER_TYPES:
        raise HTTPException(status_code=422, detail="trigger_type must be manual or schedule")
    return await _list_top_level(
        db,
        "deployment_run",
        limit,
        page=page,
        parent_id=deployment_id,
        has_error=has_error,
        trigger_type=trigger_type,
        created_at_gt=created_at_gt,
        created_at_gte=created_at_gte,
        created_at_lt=created_at_lt,
        created_at_lte=created_at_lte,
    )


@router.get("/v1/deployment_runs/{deployment_run_id}")
async def retrieve_deployment_run(deployment_run_id: str, db: AsyncSession = Depends(get_session)):
    return await _retrieve(db, deployment_run_id, "deployment_run")


@router.post("/v1/user_profiles", status_code=201)
async def create_user_profile(body: GenericBody, db: AsyncSession = Depends(get_session)):
    return await _create_top_level(db, "user_profile", body.model_dump(mode="json"))


@router.get("/v1/user_profiles")
async def list_user_profiles(
    limit: int = 50,
    page: str | None = None,
    order: str = "desc",
    db: AsyncSession = Depends(get_session),
):
    return await _list_top_level(db, "user_profile", limit, page=page, order=order)


@router.get("/v1/user_profiles/{user_profile_id}")
async def retrieve_user_profile(user_profile_id: str, db: AsyncSession = Depends(get_session)):
    return await _retrieve(db, user_profile_id, "user_profile")


@router.post("/v1/user_profiles/{user_profile_id}")
async def update_user_profile(user_profile_id: str, body: GenericBody, db: AsyncSession = Depends(get_session)):
    return await _update(
        db,
        user_profile_id,
        "user_profile",
        body.model_dump(mode="json"),
    )


async def _create_top_level(
    db: AsyncSession,
    resource_type: str,
    data: dict[str, Any],
    *,
    status: str = "active",
) -> dict[str, Any]:
    data = _normalize_resource_data(resource_type, data)
    resource = await res_q.create_resource(
        db,
        resource_type=resource_type,
        name=data.get("name") or data.get("display_name") or data.get("display_title"),
        data=data,
        status=status,
    )
    await db.commit()
    return _resource_response(resource)


async def _create_child(
    db: AsyncSession,
    resource_type: str,
    parent_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    data = _normalize_resource_data(resource_type, data)
    resource = await res_q.create_resource(
        db,
        resource_type=resource_type,
        parent_id=parent_id,
        name=data.get("name") or data.get("display_name") or data.get("display_title"),
        data=data,
    )
    await db.commit()
    return _resource_response(resource)


async def _list_top_level(
    db: AsyncSession,
    resource_type: str,
    limit: int,
    *,
    page: str | None = None,
    include_archived: bool = False,
    parent_id: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    order: str = "desc",
    has_error: bool | None = None,
    trigger_type: str | None = None,
    created_at_gt: datetime | None = None,
    created_at_gte: datetime | None = None,
    created_at_lt: datetime | None = None,
    created_at_lte: datetime | None = None,
    max_limit: int = 1000,
) -> ListResponse[dict]:
    resources = await res_q.list_resources(
        db,
        resource_type=resource_type,
        parent_id=parent_id,
        limit=1000,
        include_archived=include_archived,
    )
    resources = filter_created_at(
        resources,
        created_at_gt=created_at_gt,
        created_at_gte=created_at_gte,
        created_at_lt=created_at_lt,
        created_at_lte=created_at_lte,
    )
    if status is not None:
        resources = [resource for resource in resources if resource.status == status or resource.data.get("status") == status]
    if agent_id is not None:
        resources = [
            resource
            for resource in resources
            if _deployment_agent_response(resource.data.get("agent")).get("id") == agent_id
        ]
    if has_error is not None:
        if has_error:
            resources = [resource for resource in resources if bool(resource.data.get("error"))]
        else:
            resources = [resource for resource in resources if bool(resource.data.get("session_id"))]
    if trigger_type is not None:
        resources = [
            resource
            for resource in resources
            if (resource.data.get("trigger_context") or {}).get("type") == trigger_type
            or resource.data.get("trigger") == trigger_type
        ]
    resources = sort_by_created_at(resources, order=order)
    return paginate([_resource_response(r) for r in resources], limit=limit, page=page, max_limit=max_limit)


async def _list_child(
    db: AsyncSession,
    resource_type: str,
    parent_id: str,
    limit: int,
    *,
    page: str | None = None,
    include_archived: bool = False,
) -> ListResponse[dict]:
    resources = await res_q.list_resources(
        db,
        resource_type=resource_type,
        parent_id=parent_id,
        limit=1000,
        include_archived=include_archived,
    )
    resources = sort_by_created_at(resources, order="desc")
    return paginate([_resource_response(r) for r in resources], limit=limit, page=page)


async def _retrieve(
    db: AsyncSession,
    resource_id: str,
    resource_type: str,
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    resource = await _must_exist(db, resource_id, resource_type, parent_id=parent_id)
    return _resource_response(resource)


async def _update(
    db: AsyncSession,
    resource_id: str,
    resource_type: str,
    data: dict[str, Any],
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    resource = await _must_exist(db, resource_id, resource_type, parent_id=parent_id)
    return await _update_resource(db, resource, resource_type, data)


async def _update_resource(
    db: AsyncSession,
    resource,
    resource_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    data = _normalize_resource_data(resource_type, _merge_resource_update(resource_type, resource.data, data))
    await res_q.update_resource(
        db,
        resource,
        data=data,
        name=data.get("name") or data.get("display_name") or data.get("display_title") or resource.name,
    )
    await db.commit()
    return _resource_response(resource)


async def _archive(
    db: AsyncSession,
    resource_id: str,
    resource_type: str,
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    if resource_type == "credential":
        if parent_id is None:
            raise RuntimeError("Credential mutations require a parent Vault")
        _, resource = await _lock_vault_and_credential(db, parent_id, resource_id)
        _require_generic_credential(resource)
    else:
        resource = await _must_exist(
            db,
            resource_id,
            resource_type,
            parent_id=parent_id,
            for_update=resource_type == "vault",
        )
    if resource_type == "credential":
        await res_q.update_resource(db, resource, data=_purge_credential_secret_data(resource.data))
    elif resource_type == "vault":
        await _revoke_vault_credentials(db, resource, delete=False)
    await res_q.archive_resource(db, resource)
    await db.commit()
    return _resource_response(resource)


async def _delete(
    db: AsyncSession,
    resource_id: str,
    resource_type: str,
    public_type: str,
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    if resource_type == "credential":
        if parent_id is None:
            raise RuntimeError("Credential mutations require a parent Vault")
        _, resource = await _lock_vault_and_credential(db, parent_id, resource_id)
        _require_generic_credential(resource)
    else:
        resource = await _must_exist(
            db,
            resource_id,
            resource_type,
            parent_id=parent_id,
            for_update=resource_type == "vault",
        )
    if resource_type == "credential":
        await res_q.update_resource(db, resource, data=_purge_credential_secret_data(resource.data))
    elif resource_type == "vault":
        await _revoke_vault_credentials(db, resource, delete=True)
    await res_q.delete_resource(db, resource)
    await db.commit()
    return deleted_response(resource, public_type=public_type)


async def _revoke_vault_credentials(db: AsyncSession, vault, *, delete: bool) -> None:
    credentials = await res_q.list_child_resources_for_update(
        db,
        resource_type="credential",
        parent_id=vault.id,
        include_archived=True,
        organization_id=vault.organization_id,
    )
    for credential in credentials:
        await res_q.update_resource(
            db,
            credential,
            data=_purge_credential_secret_data(credential.data),
        )
        if delete:
            await res_q.delete_resource(db, credential)
        elif credential.archived_at is None:
            await res_q.archive_resource(db, credential)


async def _lock_vault_and_credential(db: AsyncSession, vault_id: str, credential_id: str):
    """Acquire the canonical lock order for every Credential mutation."""

    vault = await _must_exist(db, vault_id, "vault", for_update=True)
    credential = await _must_exist(
        db,
        credential_id,
        "credential",
        parent_id=vault_id,
        for_update=True,
    )
    return vault, credential


async def _must_exist(
    db: AsyncSession,
    resource_id: str,
    resource_type: str,
    *,
    parent_id: str | None = None,
    for_update: bool = False,
):
    resource = await res_q.get_resource(
        db,
        resource_id=resource_id,
        resource_type=resource_type,
        parent_id=parent_id,
        for_update=for_update,
    )
    if resource is None:
        raise HTTPException(status_code=404, detail=f"{resource_type} not found")
    return resource


def _merge_data(existing: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in update.items():
        if key == "metadata":
            merged["metadata"] = merge_metadata(merged.get("metadata") or {}, value)
        elif value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _merge_resource_update(resource_type: str, existing: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    if resource_type != "credential" or "auth" not in update:
        return _merge_data(existing, update)
    if not isinstance(update["auth"], dict):
        raise HTTPException(status_code=422, detail="credential auth must be an object")
    patch = dict(update)
    auth_patch = patch.pop("auth")
    merged = _merge_data(existing, patch)
    merged["auth"] = _merge_credential_auth((existing or {}).get("auth"), auth_patch)
    return merged


def _normalize_resource_data(resource_type: str, data: dict[str, Any]) -> dict[str, Any]:
    if resource_type == "vault":
        return _normalize_vault_data(data)
    if resource_type == "credential":
        return _normalize_credential_data(data)
    if resource_type == "memory_store":
        return _normalize_memory_store_data(data)
    if resource_type == "deployment":
        return _normalize_deployment_data(data)
    if resource_type == "deployment_run":
        return _normalize_deployment_run_data(data)
    if resource_type == "user_profile":
        return _normalize_user_profile_data(data)
    return data


def _resource_response(resource, *, view: str | None = None) -> dict[str, Any]:
    if resource.resource_type == "vault":
        return _vault_response(resource)
    if resource.resource_type == "credential":
        return _credential_response(resource)
    if resource.resource_type == "memory_store":
        return _memory_store_response(resource)
    if resource.resource_type == "memory":
        return _memory_response(resource, view=view)
    if resource.resource_type == "memory_version":
        return _memory_version_response(resource, view=view)
    if resource.resource_type == "deployment":
        return _deployment_response(resource)
    if resource.resource_type == "deployment_run":
        return _deployment_run_response(resource)
    if resource.resource_type == "user_profile":
        return _user_profile_response(resource)
    return resource_to_response(resource, public_type=resource.resource_type)


def _normalize_vault_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    display_name = _display_name_from_data(normalized, resource_name="vault", required=True)
    normalized["display_name"] = display_name
    normalized.setdefault("name", display_name)
    normalized["metadata"] = normalize_metadata(normalized.get("metadata"))
    return normalized


def _vault_response(resource) -> dict[str, Any]:
    response = resource_to_response(resource, public_type="vault")
    response["display_name"] = str(response.get("display_name") or response.get("name") or "")
    response["metadata"] = dict(response.get("metadata") or {})
    return response


def _normalize_credential_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    display_name = _display_name_from_data(normalized, resource_name="credential", required=False)
    if display_name is not None:
        normalized["display_name"] = display_name
        normalized.setdefault("name", display_name)
    if "auth" not in normalized:
        auth_type = str(normalized.pop("type", "mcp_oauth"))
        normalized["auth"] = _legacy_credential_auth(auth_type, normalized)
    normalized["auth"] = encrypt_secret_values(_normalize_credential_auth(normalized["auth"]))
    normalized["metadata"] = normalize_metadata(normalized.get("metadata"))
    return normalized


def _require_credential_create_secret(auth: dict[str, Any]) -> None:
    secret_field = {
        "environment_variable": "secret_value",
        "static_bearer": "token",
        "mcp_oauth": "access_token",
    }[_credential_auth_type(auth)]
    value = auth.get(secret_field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail=f"{auth['type']}.{secret_field} is required",
        )


def _credential_unique_key(auth: dict[str, Any]) -> tuple[str, str]:
    auth_type = _credential_auth_type(auth)
    if auth_type == "environment_variable":
        return "secret_name", str(auth.get("secret_name") or "")
    return "mcp_server_url", str(auth.get("mcp_server_url") or "")


def _display_name_from_data(data: dict[str, Any], *, resource_name: str, required: bool) -> str | None:
    display_name = data.get("display_name") or data.get("name")
    if display_name is None:
        if required:
            raise HTTPException(status_code=422, detail=f"{resource_name} display_name is required")
        return None
    if not isinstance(display_name, str):
        raise HTTPException(status_code=422, detail=f"{resource_name} display_name must be a string")
    if not display_name:
        raise HTTPException(status_code=422, detail=f"{resource_name} display_name must not be empty")
    if len(display_name) > MAX_DISPLAY_NAME_CHARS:
        raise HTTPException(status_code=422, detail=f"{resource_name} display_name must be at most 255 characters")
    return display_name


def _merge_credential_auth(existing: Any, patch: dict[str, Any]) -> dict[str, Any]:
    auth_type = _credential_auth_type(patch)
    existing_auth = dict(existing or {})
    existing_type = existing_auth.get("type")
    if existing_type and existing_type != auth_type:
        raise HTTPException(status_code=422, detail="credential auth type is immutable")
    immutable_field = "secret_name" if auth_type == "environment_variable" else "mcp_server_url"
    if immutable_field in patch:
        raise HTTPException(status_code=422, detail=f"credential {immutable_field} is immutable")
    refresh_patch = patch.get("refresh")
    if isinstance(refresh_patch, dict):
        immutable_refresh = {"client_id", "token_endpoint", "resource"}.intersection(refresh_patch)
        if immutable_refresh:
            fields = ", ".join(sorted(immutable_refresh))
            raise HTTPException(status_code=422, detail=f"credential refresh fields are immutable: {fields}")
        token_auth_patch = refresh_patch.get("token_endpoint_auth")
        existing_token_auth = (existing_auth.get("refresh") or {}).get("token_endpoint_auth")
        if isinstance(token_auth_patch, dict) and "type" in token_auth_patch:
            existing_token_type = existing_token_auth.get("type") if isinstance(existing_token_auth, dict) else None
            if existing_token_type and token_auth_patch["type"] != existing_token_type:
                raise HTTPException(status_code=422, detail="credential token endpoint auth type is immutable")
    merged = dict(existing_auth)
    for key, value in patch.items():
        if key == "refresh" and isinstance(value, dict) and isinstance(merged.get("refresh"), dict):
            merged["refresh"] = _merge_oauth_refresh(merged["refresh"], value)
        elif key == "injection_location" and isinstance(value, dict):
            current = dict(merged.get("injection_location") or {})
            current.update({child_key: child_value for child_key, child_value in value.items() if child_value is not None})
            merged["injection_location"] = current
        elif value is not None or key in {"expires_at", "refresh"}:
            merged[key] = value
    return merged


def _merge_oauth_refresh(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in patch.items():
        if (
            key == "token_endpoint_auth"
            and isinstance(value, dict)
            and isinstance(merged.get("token_endpoint_auth"), dict)
            and merged["token_endpoint_auth"].get("type") == value.get("type")
        ):
            token_endpoint_auth = dict(merged["token_endpoint_auth"])
            token_endpoint_auth.update({child_key: child_value for child_key, child_value in value.items() if child_value is not None})
            merged["token_endpoint_auth"] = token_endpoint_auth
        elif value is not None:
            merged[key] = value
    return merged


def _normalize_credential_auth(auth: Any) -> dict[str, Any]:
    if not isinstance(auth, dict):
        raise HTTPException(status_code=422, detail="credential auth must be an object")
    normalized = dict(auth)
    auth_type = _credential_auth_type(normalized)
    normalized["type"] = auth_type

    if auth_type == "environment_variable":
        _require_credential_string(normalized, "secret_name", auth_type)
        if "secret_value" in normalized and normalized["secret_value"] is not None:
            normalized["secret_value"] = _string_credential_value(normalized["secret_value"], f"{auth_type}.secret_value")
        normalized["networking"] = _normalize_credential_networking(normalized.get("networking") or {"type": "unrestricted"})
        normalized["injection_location"] = _normalize_injection_location(normalized.get("injection_location"))
        return normalized

    _require_credential_string(normalized, "mcp_server_url", auth_type)
    if auth_type == "static_bearer":
        if "token" in normalized and normalized["token"] is not None:
            normalized["token"] = _string_credential_value(normalized["token"], f"{auth_type}.token")
        return normalized

    if "access_token" in normalized and normalized["access_token"] is not None:
        normalized["access_token"] = _string_credential_value(normalized["access_token"], f"{auth_type}.access_token")
    if normalized.get("expires_at") is not None:
        normalized["expires_at"] = str(normalized["expires_at"])
    refresh = normalized.get("refresh")
    if refresh is not None:
        normalized["refresh"] = _normalize_oauth_refresh(refresh)
    return normalized


def _credential_auth_type(auth: dict[str, Any]) -> str:
    auth_type = auth.get("type")
    if auth_type not in CREDENTIAL_AUTH_TYPES:
        raise HTTPException(status_code=422, detail="credential auth type must be environment_variable, mcp_oauth, or static_bearer")
    return str(auth_type)


def _require_credential_string(data: dict[str, Any], field: str, auth_type: str) -> str:
    if field not in data or data[field] is None:
        raise HTTPException(status_code=422, detail=f"{auth_type}.{field} is required")
    value = _string_credential_value(data[field], f"{auth_type}.{field}")
    data[field] = value
    return value


def _string_credential_value(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{field} must be a string")
    if not value.strip():
        raise HTTPException(status_code=422, detail=f"{field} must not be blank")
    return value


def _normalize_credential_networking(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="environment_variable.networking must be an object")
    networking_type = value.get("type")
    if networking_type == "unrestricted":
        return {"type": "unrestricted"}
    if networking_type != "limited":
        raise HTTPException(status_code=422, detail="environment_variable.networking.type must be limited or unrestricted")
    allowed_hosts = value.get("allowed_hosts")
    if not isinstance(allowed_hosts, list):
        raise HTTPException(status_code=422, detail="environment_variable.networking.allowed_hosts must be an array")
    if len(allowed_hosts) > 16:
        raise HTTPException(status_code=422, detail="environment_variable.networking.allowed_hosts supports at most 16 hosts")
    hosts = []
    for host in allowed_hosts:
        if not isinstance(host, str) or not host or "/" in host or ":" in host:
            raise HTTPException(status_code=422, detail="environment_variable.networking.allowed_hosts entries must be bare hostnames")
        hosts.append(host)
    return {"type": "limited", "allowed_hosts": hosts}


def _normalize_oauth_refresh(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="mcp_oauth.refresh must be an object")
    refresh = dict(value)
    for field in ("client_id", "refresh_token", "token_endpoint"):
        _require_credential_string(refresh, field, "mcp_oauth.refresh")
    token_endpoint_auth = refresh.get("token_endpoint_auth")
    if not isinstance(token_endpoint_auth, dict):
        raise HTTPException(status_code=422, detail="mcp_oauth.refresh.token_endpoint_auth must be an object")
    token_endpoint_auth_type = token_endpoint_auth.get("type")
    if token_endpoint_auth_type not in CREDENTIAL_TOKEN_ENDPOINT_AUTH_TYPES:
        raise HTTPException(
            status_code=422,
            detail="mcp_oauth.refresh.token_endpoint_auth.type must be client_secret_basic, client_secret_post, or none",
        )
    normalized_auth = {"type": token_endpoint_auth_type}
    if token_endpoint_auth_type != "none":
        normalized_auth["client_secret"] = _require_credential_string(
            token_endpoint_auth,
            "client_secret",
            "mcp_oauth.refresh.token_endpoint_auth",
        )
    refresh["token_endpoint_auth"] = normalized_auth
    for optional in ("resource", "scope"):
        if refresh.get(optional) is not None:
            refresh[optional] = _string_credential_value(refresh[optional], f"mcp_oauth.refresh.{optional}")
    return refresh


def _legacy_credential_auth(auth_type: str, data: dict[str, Any]) -> dict[str, Any]:
    if auth_type == "static_bearer":
        return {
            "type": "static_bearer",
            "mcp_server_url": data.get("mcp_server_url") or "https://example.invalid/mcp",
            "token": data.get("token") or data.get("api_key") or "",
        }
    if auth_type == "environment_variable":
        return {
            "type": "environment_variable",
            "secret_name": data.get("secret_name") or data.get("name") or "SECRET",
            "secret_value": data.get("secret_value") or "",
            "networking": data.get("networking") or {"type": "unrestricted"},
        }
    return {
        "type": "mcp_oauth",
        "mcp_server_url": data.get("mcp_server_url") or "https://example.invalid/mcp",
        "access_token": data.get("access_token") or "",
        "expires_at": data.get("expires_at"),
        "refresh": data.get("refresh"),
    }


def _credential_response(resource) -> dict[str, Any]:
    response = resource_to_response(resource, public_type="vault_credential")
    response["vault_id"] = resource.parent_id
    response["display_name"] = response.get("display_name") or response.get("name")
    response["metadata"] = dict(response.get("metadata") or {})
    response["auth"] = _credential_auth_response((resource.data or {}).get("auth") or {})
    return response


def _model_credential_response(resource) -> ModelCredentialResponse:
    data = resource.data or {}
    provider_id = _model_credential_provider_id(resource)
    if provider_id is None:
        raise HTTPException(status_code=404, detail="Model credential not found")
    return ModelCredentialResponse(
        id=resource.id,
        vault_id=str(resource.parent_id or ""),
        model_provider=provider_id,
        display_name=str(data.get("display_name") or resource.name or ""),
        metadata=dict(data.get("metadata") or {}),
        created_at=resource.created_at,
        updated_at=resource.updated_at,
        archived_at=resource.archived_at,
    )


def _model_credential_provider_id(resource) -> str | None:
    """Identify native BYOK rows without returning their generic auth shape.

    New rows carry an explicit internal kind. The provider plus environment
    credential shape also recognizes rows created before that marker existed,
    even if an operator later removes the provider from the live catalog.
    """

    data = resource.data or {}
    credential_kind = data.get("credential_kind")
    if credential_kind not in {None, MODEL_CREDENTIAL_KIND}:
        return None
    provider_id = data.get("model_provider")
    if not isinstance(provider_id, str) or not provider_id:
        return None
    auth = data.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "environment_variable":
        return None
    return provider_id


def _reject_reserved_model_credential_fields(data: dict[str, Any]) -> None:
    if RESERVED_MODEL_CREDENTIAL_FIELDS & data.keys():
        raise HTTPException(
            status_code=422,
            detail="Use the model_credentials API for model-provider credentials",
        )


def _require_generic_credential(resource) -> None:
    if _model_credential_provider_id(resource) is not None:
        raise HTTPException(status_code=404, detail="credential not found")


def _credential_auth_response(auth: dict[str, Any]) -> dict[str, Any]:
    auth_type = str(auth.get("type") or "mcp_oauth")
    if auth_type == "static_bearer":
        return {
            "type": "static_bearer",
            "mcp_server_url": str(auth.get("mcp_server_url") or "https://example.invalid/mcp"),
        }
    if auth_type == "environment_variable":
        return {
            "type": "environment_variable",
            "secret_name": str(auth.get("secret_name") or "SECRET"),
            "networking": _credential_networking_response(auth.get("networking")),
            "injection_location": _normalize_injection_location(auth.get("injection_location")),
        }
    response = {
        "type": "mcp_oauth",
        "mcp_server_url": str(auth.get("mcp_server_url") or "https://example.invalid/mcp"),
        "expires_at": auth.get("expires_at"),
        "refresh": _credential_refresh_response(auth.get("refresh")),
    }
    return {key: value for key, value in response.items() if value is not None}


def _credential_refresh_response(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    token_endpoint_auth = value.get("token_endpoint_auth") or {}
    auth_type = token_endpoint_auth.get("type")
    if auth_type not in CREDENTIAL_TOKEN_ENDPOINT_AUTH_TYPES:
        auth_type = "none"
    response = {
        "client_id": str(value.get("client_id") or ""),
        "token_endpoint": str(value.get("token_endpoint") or ""),
        "token_endpoint_auth": {"type": auth_type},
        "resource": value.get("resource"),
        "scope": value.get("scope"),
    }
    return {key: item for key, item in response.items() if item is not None}


def _credential_networking_response(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("type") == "limited":
        return {"type": "limited", "allowed_hosts": list(value.get("allowed_hosts") or [])}
    return {"type": "unrestricted"}


def _normalize_injection_location(value: Any) -> dict[str, bool]:
    if value is None:
        return {"body": True, "header": True}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="environment_variable.injection_location must be an object")
    unknown = set(value) - {"body", "header"}
    if unknown:
        raise HTTPException(status_code=422, detail="injection_location supports only body and header")
    normalized = {"body": True, "header": True}
    for key in ("body", "header"):
        if key in value:
            if not isinstance(value[key], bool):
                raise HTTPException(status_code=422, detail=f"injection_location.{key} must be a boolean")
            normalized[key] = value[key]
    return normalized


def _purge_credential_secret_data(data: dict[str, Any] | None) -> dict[str, Any]:
    purged = _purge_secret_values(data or {})
    if isinstance(purged, dict):
        purged["secrets_purged_at"] = utcnow().isoformat()
    return purged if isinstance(purged, dict) else {}


def _purge_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if is_secret_key(key):
                result[key] = None
            else:
                result[key] = _purge_secret_values(child)
        return result
    if isinstance(value, list):
        return [_purge_secret_values(item) for item in value]
    return value


def _normalize_memory_store_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    normalized["name"] = _memory_store_name(normalized.get("name"))
    normalized.setdefault("description", "")
    normalized["description"] = _memory_store_description(normalized.get("description"))
    normalized["metadata"] = normalize_metadata(normalized.get("metadata"))
    return normalized


def _memory_store_name(value: Any) -> str:
    if value is None:
        raise HTTPException(status_code=422, detail="memory_store name is required")
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="memory_store name must be a string")
    if not value:
        raise HTTPException(status_code=422, detail="memory_store name must not be empty")
    if len(value) > MAX_DISPLAY_NAME_CHARS:
        raise HTTPException(status_code=422, detail="memory_store name must be at most 255 characters")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise HTTPException(status_code=422, detail="memory_store name must not contain control characters")
    return value


def _memory_store_description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="memory_store description must be a string")
    if len(value) > MAX_MEMORY_STORE_DESCRIPTION_CHARS:
        raise HTTPException(status_code=422, detail="memory_store description must be at most 1024 characters")
    return value


def _memory_store_response(resource) -> dict[str, Any]:
    response = resource_to_response(resource, public_type="memory_store")
    response["description"] = response.get("description") or ""
    response["metadata"] = dict(response.get("metadata") or {})
    return {
        key: value
        for key, value in response.items()
        if key in MemoryStoreResponse.model_fields
    }


def _memory_payload(data: dict[str, Any]) -> dict[str, Any]:
    path = _normalize_memory_path(data.get("path"))
    now = utcnow().isoformat()
    actor = str(data.get("actor") or data.get("updated_by") or "api")
    memory = dict(data)
    memory["path"] = path
    memory["path_key"] = _path_key(path)
    memory["content"] = "" if memory.get("content") is None else str(memory.get("content"))
    _enforce_memory_content_limit(memory["content"])
    memory.update(_content_metadata(memory["content"]))
    memory["version"] = 1
    memory["created_by"] = actor
    memory["updated_by"] = actor
    memory["created_at"] = now
    memory["updated_at"] = now
    memory.setdefault("metadata", {})
    memory.setdefault("redacted", False)
    memory.pop("actor", None)
    return memory


def _merge_memory_data(existing: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    requested_actor = update.pop("actor", None)
    requested_updated_by = update.pop("updated_by", None)
    actor = str(requested_actor or requested_updated_by or merged.get("updated_by") or "api")
    if "path" in update:
        path = _normalize_memory_path(update["path"])
        merged["path"] = path
        merged["path_key"] = _path_key(path)
    for key, value in update.items():
        if key in {"path_key", "version", "created_at", "created_by", "memory_version_id"}:
            continue
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    if "content" in update:
        content = "" if merged.get("content") is None else str(merged.get("content"))
        _enforce_memory_content_limit(content)
        merged["content"] = content
        merged.update(_content_metadata(content))
    merged["version"] = int(merged.get("version") or 1) + 1
    merged["updated_by"] = actor
    merged["updated_at"] = utcnow().isoformat()
    if merged.get("metadata") is None:
        merged["metadata"] = {}
    return merged


def _memory_requested_state_matches_current(existing: dict[str, Any] | None, update: dict[str, Any]) -> bool:
    current = dict(existing or {})
    requested_path = current.get("path")
    if "path" in update:
        requested_path = _normalize_memory_path(update["path"])

    requested_content = "" if current.get("content") is None else str(current.get("content"))
    if "content" in update:
        requested_content = "" if update["content"] is None else str(update["content"])

    current_path = _normalize_memory_path(current.get("path"))
    current_content = "" if current.get("content") is None else str(current.get("content"))
    return requested_path == current_path and requested_content == current_content


def _memory_has_material_change(existing: dict[str, Any] | None, merged: dict[str, Any]) -> bool:
    ignored = {
        "content_sha256",
        "content_size_bytes",
        "created_at",
        "created_by",
        "memory_version_id",
        "session_id",
        "updated_at",
        "updated_by",
        "version",
    }
    existing_material = {key: value for key, value in dict(existing or {}).items() if key not in ignored}
    merged_material = {key: value for key, value in merged.items() if key not in ignored}
    return existing_material != merged_material


async def _must_write_memory_store(db: AsyncSession, memory_store_id: str):
    store = await _must_exist(db, memory_store_id, "memory_store")
    if store.archived_at is not None:
        raise HTTPException(status_code=409, detail="Memory store is archived")
    return store


async def _ensure_memory_store_capacity(db: AsyncSession, memory_store_id: str) -> None:
    memories = await res_q.list_resources(
        db,
        resource_type="memory",
        parent_id=memory_store_id,
        limit=MAX_MEMORIES_PER_STORE + 1,
    )
    if len(memories) >= MAX_MEMORIES_PER_STORE:
        raise HTTPException(status_code=409, detail="Memory store has reached the 2000 memory limit")


def _enforce_memory_content_limit(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_MEMORY_CONTENT_BYTES:
        raise HTTPException(status_code=413, detail="Memory content exceeds maximum size of 102400 bytes")


async def _find_memory_by_path(db: AsyncSession, memory_store_id: str, path_key: str):
    return await res_q.get_resource_by_name(
        db,
        resource_type="memory",
        parent_id=memory_store_id,
        name=path_key,
    )


async def _create_memory_version(
    db: AsyncSession,
    memory,
    *,
    version: int,
    actor: str,
    operation: str,
    data: dict[str, Any] | None = None,
):
    version_data = data or memory.data
    snapshot = _memory_snapshot(version_data)
    return await res_q.create_resource(
        db,
        resource_type="memory_version",
        parent_id=memory.id,
        version=version,
        data={
            "memory_store_id": memory.parent_id,
            "memory_id": memory.id,
            "memory_version": version,
            "path": version_data.get("path"),
            "path_key": version_data.get("path_key"),
            "content": snapshot.get("content"),
            "content_sha256": snapshot.get("content_sha256"),
            "content_size_bytes": snapshot.get("content_size_bytes"),
            "snapshot": snapshot,
            "actor": actor,
            "created_by": _api_actor(actor),
            "session_id": version_data.get("session_id"),
            "operation": operation,
            "redacted": False,
        },
    )


def _memory_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in {"path_key"}
    }


def _normalize_memory_path(value: Any) -> str:
    if value is None:
        raise HTTPException(status_code=422, detail="Memory path is required")
    if isinstance(value, str):
        text = value.strip()
        _validate_memory_path_text(text)
        parts = text.removeprefix("/").split("/")
    elif isinstance(value, list):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise HTTPException(status_code=422, detail="Memory path must be a string or array")
    if not parts:
        raise HTTPException(status_code=422, detail="Memory path cannot be empty")
    if any("/" in part for part in parts):
        raise HTTPException(status_code=422, detail="Memory path array items cannot contain '/'")
    path = "/" + "/".join(parts)
    _validate_memory_path_text(path)
    return path


def _normalize_memory_path_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text == "/":
        return "/"
    if text != "/" and text.endswith("/"):
        text = text.rstrip("/")
    return _normalize_memory_path(text)


def _validate_memory_path_text(path: str) -> None:
    if not path.startswith("/"):
        raise HTTPException(status_code=422, detail="Memory path must start with '/'")
    if len(path.encode("utf-8")) > MAX_MEMORY_PATH_BYTES:
        raise HTTPException(status_code=422, detail="Memory path must be at most 1024 bytes")
    if unicodedata.normalize("NFC", path) != path:
        raise HTTPException(status_code=422, detail="Memory path must be NFC-normalized")
    parts = path.split("/")
    if len(parts) == 1 or parts[1:] == [""]:
        raise HTTPException(status_code=422, detail="Memory path cannot be empty")
    if any(part == "" for part in parts[1:]):
        raise HTTPException(status_code=422, detail="Memory path must not contain empty segments")
    if any(part in {".", ".."} for part in parts[1:]):
        raise HTTPException(status_code=422, detail="Memory path must not contain '.' or '..' segments")
    if any(_has_control_or_format_characters(part) for part in parts[1:]):
        raise HTTPException(status_code=422, detail="Memory path must not contain control or format characters")


def _has_control_or_format_characters(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)


def _path_key(path: str) -> str:
    return path.strip("/")


def _content_metadata(content: str) -> dict[str, Any]:
    content_bytes = content.encode("utf-8")
    return {
        "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
        "content_size_bytes": len(content_bytes),
    }


def _memory_response(resource, *, view: str | None = None) -> dict[str, Any]:
    view = _normalize_memory_view(view)
    response = resource_to_response(resource, public_type="memory")
    content = response.get("content")
    if content is None and not response.get("redacted"):
        content = ""
    response["memory_store_id"] = resource.parent_id
    response["path"] = _normalize_memory_path(response.get("path"))
    response["path_key"] = _path_key(response["path"])
    if content is not None:
        response.update(_content_metadata(str(content)))
    response.setdefault("content_sha256", _content_metadata("")["content_sha256"])
    response.setdefault("content_size_bytes", 0)
    response["memory_version_id"] = str(response.get("memory_version_id") or "")
    if view == "basic":
        response["content"] = None
    return {
        key: value
        for key, value in response.items()
        if key in MemoryResponse.model_fields
    }


def _memory_version_response(resource, *, view: str | None = None) -> dict[str, Any]:
    view = _normalize_memory_view(view)
    response = resource_to_response(resource, public_type="memory_version")
    data = resource.data or {}
    snapshot = dict(data.get("snapshot") or {})
    for private_field in ("snapshot", "actor", "session_id", "path_key"):
        response.pop(private_field, None)
    redacted = bool(data.get("redacted") or data.get("redacted_at"))
    response["memory_store_id"] = data.get("memory_store_id") or snapshot.get("memory_store_id")
    response["memory_id"] = data.get("memory_id") or resource.parent_id
    operation = _memory_version_operation(data.get("operation"))
    response["operation"] = operation
    response["created_by"] = data.get("created_by") or _api_actor(str(data.get("actor") or "api"))
    response.pop("session_id", None)
    response["redacted_at"] = data.get("redacted_at")
    if redacted:
        response["content"] = None
        response["path"] = None
        response["content_sha256"] = None
        response["content_size_bytes"] = None
    elif operation == "deleted":
        response["content"] = None
        response["path"] = _normalize_memory_path(data.get("path") or snapshot.get("path"))
        response["content_sha256"] = None
        response["content_size_bytes"] = None
    else:
        content = data.get("content", snapshot.get("content"))
        response["content"] = None if view == "basic" else content
        response["path"] = _normalize_memory_path(data.get("path") or snapshot.get("path"))
        response["content_sha256"] = data.get("content_sha256") or snapshot.get("content_sha256")
        response["content_size_bytes"] = data.get("content_size_bytes") or snapshot.get("content_size_bytes")
    return {
        key: value
        for key, value in response.items()
        if key in MemoryVersionResponse.model_fields
    }


def _memory_version_page(
    resources: list,
    *,
    view: str | None,
    page_size: int,
    offset: int,
) -> ListResponse[dict[str, Any]]:
    has_more = len(resources) > page_size
    data = [_resource_response(resource, view=view) for resource in resources[:page_size]]
    next_page = encode_page_offset(offset + page_size) if has_more else None
    return ListResponse.from_items(data, has_more=has_more, next_page=next_page)


def _normalize_memory_view(view: str | None) -> str | None:
    if view is None:
        return None
    normalized = view.lower()
    if normalized not in MEMORY_VIEWS:
        raise HTTPException(status_code=422, detail="view must be basic or full")
    return normalized


def _memory_version_operation(value: Any) -> str:
    if value in {"created", "modified", "deleted"}:
        return str(value)
    if value == "create":
        return "created"
    if value == "delete":
        return "deleted"
    return "modified"


def _sort_memories(resources: list, *, order: str, order_by: str) -> list:
    order = normalize_sort_order(order, default="asc")
    if order_by not in {"path", "created_at"}:
        raise HTTPException(status_code=422, detail="order_by must be path or created_at")
    reverse = order == "desc"
    if order_by == "created_at":
        return sort_by_created_at(resources, order=order)
    return sorted(resources, key=lambda resource: str(resource.data.get("path_key") or ""), reverse=reverse)


def _memory_list_items_with_depth(
    resources: list,
    *,
    view: str | None,
    depth: int,
    path_prefix: str | None,
    order: str,
) -> list[dict[str, Any]]:
    if depth < 0:
        raise HTTPException(status_code=422, detail="Memory list depth must be non-negative")

    prefix_key = _path_key(_normalize_memory_path_prefix(path_prefix)) if path_prefix is not None else ""
    base_parts = prefix_key.split("/") if prefix_key else []
    items_by_path: dict[str, dict[str, Any]] = {}

    for resource in resources:
        path_key = _memory_resource_path_key(resource)
        if not path_key:
            continue
        parts = path_key.split("/")
        if prefix_key:
            if path_key == prefix_key:
                relative_parts: list[str] = []
            elif path_key.startswith(f"{prefix_key}/"):
                relative_parts = parts[len(base_parts) :]
            else:
                continue
        else:
            relative_parts = parts

        if len(relative_parts) <= depth:
            memory = _resource_response(resource, view=view)
            items_by_path[memory["path"]] = memory
            continue

        rollup_parts = parts[: len(base_parts) + depth]
        prefix_path = "/" if not rollup_parts else f"/{'/'.join(rollup_parts)}/"
        items_by_path.setdefault(prefix_path, {"type": "memory_prefix", "path": prefix_path})

    reverse = order == "desc"
    return [items_by_path[path] for path in sorted(items_by_path, reverse=reverse)]


def _memory_resource_path_key(resource) -> str:
    data = resource.data or {}
    value = data.get("path_key") or resource.name or ""
    return str(value).strip("/")


def _api_actor(api_key_id: str) -> dict[str, str]:
    return {"type": "api_actor", "api_key_id": api_key_id}


class DeploymentRunCreationError(RuntimeError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error = {"type": error_type, "message": message}


async def _scheduled_deployment_due_at(db: AsyncSession, deployment, *, now: datetime) -> datetime | None:
    if deployment.status == "paused" or (deployment.data or {}).get("status") == "paused":
        return None
    schedule = (deployment.data or {}).get("schedule") or {}
    if not isinstance(schedule, dict) or not schedule.get("enabled", True):
        return None
    due_candidates = [
        due_at
        for due_at in (_parse_datetime(value) for value in schedule.get("upcoming_runs_at") or [])
        if due_at is not None and due_at <= now
    ]
    if not due_candidates:
        return None
    due_at = min(due_candidates)
    if await _scheduled_deployment_run_exists(db, deployment.id, due_at):
        return None
    return due_at


async def _scheduled_deployment_run_exists(db: AsyncSession, deployment_id: str, scheduled_for: datetime) -> bool:
    runs = await res_q.list_resources(
        db,
        resource_type="deployment_run",
        parent_id=deployment_id,
        limit=1000,
    )
    for run in runs:
        data = run.data or {}
        if data.get("trigger") == "schedule" and _parse_datetime(data.get("scheduled_for")) == scheduled_for:
            return True
    return False


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def _ensure_deployment_mutable(deployment) -> None:
    if deployment.archived_at is not None:
        raise HTTPException(status_code=409, detail="Deployment is archived")


async def _archive_if_deployment_agent_unusable(
    db: AsyncSession,
    deployment,
    deployment_data: dict[str, Any],
) -> None:
    agent_id, _version = _deployment_agent_ref(deployment_data.get("agent"))
    agent = await agents_q.get_agent(db, agent_id)
    if agent is not None and agent.archived_at is None:
        return
    await res_q.archive_resource(db, deployment)
    await db.commit()
    if agent is None:
        raise HTTPException(status_code=409, detail="Deployment agent was not found; deployment archived")
    raise HTTPException(status_code=409, detail="Deployment agent is archived; deployment archived")


def _normalize_deployment_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    status = str(normalized.get("status") or "active")
    if status not in {"active", "paused"}:
        raise HTTPException(status_code=422, detail="Deployment status must be active or paused")
    normalized["status"] = status
    if status == "paused":
        normalized.setdefault("paused_reason", {"type": "manual"})
    else:
        normalized.pop("paused_reason", None)
    normalized["agent"] = _deployment_agent_reference_input(normalized.get("agent"))
    normalized.setdefault("environment_id", "")
    normalized.setdefault("initial_events", [])
    normalized["resources"] = _normalize_deployment_resources(normalized.get("resources"))
    normalized["vault_ids"] = _normalize_deployment_vault_ids(normalized.get("vault_ids"))
    normalized["metadata"] = normalize_metadata(normalized.get("metadata"))

    schedule = normalized.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            raise HTTPException(status_code=422, detail="Deployment schedule must be an object")
        schedule_type = str(schedule.get("type") or "cron")
        if schedule_type != "cron":
            raise HTTPException(status_code=422, detail="Only cron deployment schedules are supported")
        expression = str(schedule.get("expression") or schedule.get("cron") or "").strip()
        if not _valid_cron_expression(expression):
            raise HTTPException(status_code=422, detail="Deployment cron schedule must have 5 fields")
        timezone = str(schedule.get("timezone") or "UTC")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise HTTPException(status_code=422, detail=f"Unknown deployment schedule timezone: {timezone}") from exc
        normalized["schedule"] = {
            **schedule,
            "type": "cron",
            "expression": expression,
            "cron": expression,
            "timezone": timezone,
            "enabled": bool(schedule.get("enabled", True)),
            "upcoming_runs_at": _upcoming_cron_runs(expression, timezone),
        }
    return normalized


async def _validate_deployment_definition(db: AsyncSession, data: dict[str, Any]) -> None:
    name = str(data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Deployment name is required")

    agent_ref = await _resolve_deployment_agent_reference(db, data.get("agent"))
    data["agent"] = agent_ref
    agent_id = str(agent_ref.get("id") or "")
    if not agent_id:
        raise HTTPException(status_code=422, detail="Deployment agent is required")

    environment_id = str(data.get("environment_id") or "")
    if not environment_id:
        raise HTTPException(status_code=422, detail="Deployment environment_id is required")
    environment = await env_q.get_environment(db, environment_id)
    if environment is None or environment.deleted_at is not None or environment.archived_at is not None:
        raise HTTPException(status_code=422, detail="Deployment environment not found")

    _validate_deployment_initial_events(data.get("initial_events") or [])


def _deployment_agent_reference_input(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"type": "agent", "id": value}
    if isinstance(value, dict):
        agent_id = value.get("id") or value.get("agent_id") or ""
        normalized: dict[str, Any] = {"type": "agent", "id": str(agent_id)}
        if value.get("version") is not None:
            normalized["version"] = int(value["version"])
        return normalized
    return {"type": "agent", "id": ""}


async def _resolve_deployment_agent_reference(db: AsyncSession, value: Any) -> dict[str, Any]:
    agent_ref = _deployment_agent_reference_input(value)
    agent_id = str(agent_ref.get("id") or "")
    if not agent_id:
        return agent_ref
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None or agent.archived_at is not None:
        raise HTTPException(status_code=422, detail="Deployment agent not found")
    version = agent.active_version if agent_ref.get("version") is None else int(agent_ref["version"])
    agent_version = await agents_q.get_agent_version(db, agent_id=agent.id, version=version)
    if agent_version is None:
        raise HTTPException(status_code=422, detail="Deployment agent version not found")
    return {"type": "agent", "id": agent.id, "version": version}


def _validate_deployment_initial_events(initial_events: list[Any]) -> None:
    if not isinstance(initial_events, list):
        raise HTTPException(status_code=422, detail="Deployment initial_events must be an array")
    if not initial_events:
        raise HTTPException(status_code=422, detail="Deployment initial_events must contain at least one event")
    if len(initial_events) > 50:
        raise HTTPException(status_code=422, detail="Deployment initial_events supports at most 50 events")
    event_types: list[str] = []
    for raw_event in initial_events:
        if not isinstance(raw_event, dict):
            raise HTTPException(status_code=422, detail="Deployment initial_events entries must be objects")
        event_type = str(raw_event.get("type") or "")
        if event_type not in {"system.message", "user.define_outcome", "user.message"}:
            raise HTTPException(status_code=422, detail="Unsupported deployment initial event type")
        if event_type == "user.define_outcome":
            validate_user_define_outcome_event(raw_event)
        event_types.append(event_type)
    validate_system_message_batch(event_types)
    if "user.message" not in event_types:
        raise HTTPException(status_code=422, detail="Deployment initial_events must include a user.message event")


def _normalize_deployment_resources(resources: Any) -> list[Any]:
    if resources is None:
        return []
    if not isinstance(resources, list):
        raise HTTPException(status_code=422, detail="Deployment resources must be an array")
    if len(resources) > MAX_DEPLOYMENT_RESOURCES:
        raise HTTPException(status_code=422, detail="Deployment resources supports at most 500 resources")
    return list(resources)


def _normalize_deployment_vault_ids(vault_ids: Any) -> list[Any]:
    if vault_ids is None:
        return []
    if not isinstance(vault_ids, list):
        raise HTTPException(status_code=422, detail="Deployment vault_ids must be an array")
    if len(vault_ids) > MAX_DEPLOYMENT_VAULT_IDS:
        raise HTTPException(status_code=422, detail="Deployment vault_ids supports at most 50 vaults")
    return list(vault_ids)


def _valid_cron_expression(expression: str) -> bool:
    parts = expression.split()
    if len(parts) != 5 or not all(parts):
        return False
    try:
        _cron_field_values(parts[0], minimum=0, maximum=59)
        _cron_field_values(parts[1], minimum=0, maximum=23)
        _cron_field_values(parts[2], minimum=1, maximum=31)
        _cron_field_values(parts[3], minimum=1, maximum=12)
        _cron_field_values(parts[4], minimum=0, maximum=7)
    except ValueError:
        return False
    return True


def _upcoming_cron_runs(expression: str, timezone: str, *, count: int = 5) -> list[str]:
    parts = expression.split()
    if len(parts) != 5:
        return []
    minutes = _cron_field_values(parts[0], minimum=0, maximum=59)
    hours = _cron_field_values(parts[1], minimum=0, maximum=23)
    days = _cron_field_values(parts[2], minimum=1, maximum=31)
    months = _cron_field_values(parts[3], minimum=1, maximum=12)
    weekdays = _cron_field_values(parts[4], minimum=0, maximum=7)
    tz = ZoneInfo(timezone)
    current = utcnow().astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    matches: list[str] = []
    for _ in range(366 * 24 * 60):
        cron_weekday = (current.weekday() + 1) % 7
        if (
            current.minute in minutes
            and current.hour in hours
            and current.day in days
            and current.month in months
            and (cron_weekday in weekdays or (cron_weekday == 0 and 7 in weekdays))
        ):
            matches.append(current.astimezone(ZoneInfo("UTC")).isoformat())
            if len(matches) >= count:
                return matches
        current += timedelta(minutes=1)
    return matches


def _cron_field_values(part: str, *, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for item in part.split(","):
        item = item.strip()
        if not item:
            raise ValueError("empty cron field item")
        step = 1
        if "/" in item:
            item, raw_step = item.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if item == "*":
            start, end = minimum, maximum
        elif "-" in item:
            raw_start, raw_end = item.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(item)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron field value out of range")
        values.update(range(start, end + 1, step))
    return values


def _normalize_deployment_run_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    normalized["agent"] = _deployment_agent_response(normalized.get("agent"))
    normalized.setdefault("trigger_context", _trigger_context(normalized))
    normalized.setdefault("error", None)
    return normalized


def _deployment_response(resource) -> dict[str, Any]:
    response = resource_to_response(resource, public_type="deployment")
    data = resource.data or {}
    response["agent"] = _deployment_agent_response(data.get("agent"))
    response["environment_id"] = str(data.get("environment_id") or "")
    response["initial_events"] = list(data.get("initial_events") or [])
    response["metadata"] = dict(data.get("metadata") or {})
    response["resources"] = _deployment_resources_response(data.get("resources") or [])
    response["vault_ids"] = list(data.get("vault_ids") or [])
    response["description"] = data.get("description")
    response["schedule"] = _deployment_schedule_response(data.get("schedule"))
    response["status"] = "paused" if resource.status == "paused" or data.get("status") == "paused" else "active"
    response["paused_reason"] = data.get("paused_reason")
    return response


def _deployment_run_response(resource) -> dict[str, Any]:
    response = resource_to_response(resource, public_type="deployment_run")
    data = resource.data or {}
    response["deployment_id"] = data.get("deployment_id") or resource.parent_id
    response["agent"] = _deployment_agent_response(data.get("agent"))
    response["session_id"] = data.get("session_id")
    response["error"] = data.get("error")
    response["trigger_context"] = data.get("trigger_context") or _trigger_context(data)
    return response


def _deployment_resources_response(resources: list[Any]) -> list[dict[str, Any]]:
    response: list[dict[str, Any]] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("type")
        if resource_type == "github_repository":
            item = {
                "type": "github_repository",
                "url": str(resource.get("url") or ""),
            }
            if resource.get("mount_path") is not None:
                item["mount_path"] = resource["mount_path"]
            if resource.get("checkout") is not None:
                item["checkout"] = resource["checkout"]
            response.append(item)
        elif resource_type == "file":
            item = {
                "type": "file",
                "file_id": str(resource.get("file_id") or ""),
            }
            if resource.get("mount_path") is not None:
                item["mount_path"] = resource["mount_path"]
            response.append(item)
        elif resource_type == "memory_store":
            item = {
                "type": "memory_store",
                "memory_store_id": str(resource.get("memory_store_id") or ""),
            }
            if resource.get("access") is not None:
                item["access"] = resource["access"]
            if resource.get("instructions") is not None:
                item["instructions"] = resource["instructions"]
            response.append(item)
        else:
            item = dict(resource)
            item.pop("authorization_token", None)
            response.append(item)
    return response


def _deployment_agent_response(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"type": "agent", "id": value, "version": 1}
    if isinstance(value, dict):
        agent_id = value.get("id") or value.get("agent_id") or ""
        version = int(value.get("version") or 1)
        return {"type": "agent", "id": str(agent_id), "version": version}
    return {"type": "agent", "id": "", "version": 1}


def _deployment_schedule_response(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    expression = str(value.get("expression") or value.get("cron") or "").strip()
    if not expression:
        return None
    return {
        "type": "cron",
        "expression": expression,
        "cron": expression,
        "timezone": str(value.get("timezone") or "UTC"),
        "enabled": bool(value.get("enabled", True)),
        "last_run_at": value.get("last_run_at"),
        "upcoming_runs_at": list(value.get("upcoming_runs_at") or []),
    }


def _trigger_context(value: dict[str, Any]) -> dict[str, Any]:
    trigger = value.get("trigger") or value.get("trigger_type") or "manual"
    if trigger == "schedule":
        return {"type": "schedule", "scheduled_at": value.get("scheduled_for") or utcnow()}
    return {"type": "manual"}


def _normalize_user_profile_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    normalized.setdefault("relationship", "external")
    normalized["metadata"] = normalize_metadata(normalized.get("metadata"))
    _validate_user_profile_data(normalized)
    normalized.setdefault("trust_grants", {})
    return normalized


def _validate_user_profile_data(data: dict[str, Any]) -> None:
    relationship = data.get("relationship")
    if relationship not in USER_PROFILE_RELATIONSHIPS:
        raise HTTPException(status_code=422, detail="user_profile relationship must be external, internal, or resold")

    for field in ("external_id", "name"):
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"user_profile {field} must be a string")
        if len(value) > MAX_USER_PROFILE_FIELD_CHARS:
            raise HTTPException(status_code=422, detail=f"user_profile {field} must be at most 255 characters")

    if relationship == "resold" and not data.get("name"):
        raise HTTPException(status_code=422, detail="user_profile name is required when relationship is resold")


def _user_profile_response(resource) -> dict[str, Any]:
    response = resource_to_response(resource, public_type="user_profile")
    response["relationship"] = response.get("relationship") or "external"
    response["metadata"] = dict(response.get("metadata") or {})
    response["trust_grants"] = dict(response.get("trust_grants") or {})
    return response


async def _maybe_create_deployment_session(db: AsyncSession, deployment, run, run_input: dict[str, Any]):
    data = deployment.data or {}
    agent_ref = run_input.get("agent") or data.get("agent")
    environment_id = run_input.get("environment_id") or data.get("environment_id")
    if not agent_ref or not environment_id:
        return None

    agent_id, requested_version = _deployment_agent_ref(agent_ref)
    agent = await agents_q.get_agent(db, agent_id)
    if agent is None:
        raise DeploymentRunCreationError("agent_not_found_error", f"agent `{agent_id}` was not found")
    if agent.archived_at is not None:
        raise DeploymentRunCreationError("agent_archived_error", f"agent `{agent_id}` is archived")
    version = requested_version or agent.active_version
    agent_version = await agents_q.get_agent_version(db, agent_id=agent.id, version=version)
    if agent_version is None:
        raise DeploymentRunCreationError(
            "agent_version_not_found_error",
            f"agent `{agent_id}` version `{version}` was not found",
        )
    environment = await env_q.get_environment(db, str(environment_id))
    if environment is None or environment.deleted_at is not None:
        raise DeploymentRunCreationError(
            "environment_not_found_error",
            f"environment `{environment_id}` was not found",
        )
    if environment.archived_at is not None:
        raise DeploymentRunCreationError(
            "environment_archived_error",
            f"environment `{environment_id}` is archived",
        )

    metadata = dict(run_input.get("metadata") or {})
    metadata.update({"deployment_id": deployment.id, "deployment_run_id": run.id})
    vault_input = run_input["vault_ids"] if "vault_ids" in run_input else data.get("vault_ids")
    vault_ids = await _validate_deployment_vault_ids(db, vault_input)
    agent_config = await resolve_session_agent_config(
        db,
        agent_version,
        None,
        organization_id=agent.organization_id,
    )
    try:
        async with db.begin_nested():
            session = await sessions_q.create_session(
                db,
                agent=agent,
                agent_version=version,
                environment=environment,
                title=run_input.get("title") or data.get("title") or data.get("name") or deployment.name,
                metadata=metadata,
                vault_ids=vault_ids,
                agent_config=agent_config,
            )
            resource_input = run_input["resources"] if "resources" in run_input else data.get("resources")
            for resource_data in _normalize_deployment_resources(resource_input):
                await create_session_resource(
                    db,
                    session,
                    resource_data,
                    allowed_types={"file", "github_repository", "memory_store"},
                )
            await _append_deployment_session_events(
                db,
                session,
                run_input.get("initial_events") or data.get("initial_events") or [],
            )
            await provision_session_sandbox(
                db,
                session=session,
                agent_version=agent_version,
                environment_config=environment.config,
            )
    except ModelCredentialRequiredError as exc:
        raise DeploymentRunCreationError(
            "model_credential_required",
            str(exc),
        ) from exc
    except (SandboxLifecycleConfigurationError, SandboxProviderError) as exc:
        raise DeploymentRunCreationError(
            "sandbox_provision_error",
            "The deployment session sandbox could not be provisioned",
        ) from exc
    return session


async def _validate_deployment_vault_ids(db: AsyncSession, vault_ids: Any) -> list[str]:
    vault_ids = _normalize_deployment_vault_ids(vault_ids)
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_id in vault_ids:
        vault_id = str(raw_id or "")
        if not vault_id:
            raise HTTPException(status_code=422, detail="vault_ids must not contain empty values")
        if vault_id in seen:
            continue
        vault = await res_q.get_resource(db, resource_id=vault_id, resource_type="vault")
        if vault is None or vault.archived_at is not None:
            raise HTTPException(status_code=404, detail=f"Vault not found: {vault_id}")
        resolved.append(vault_id)
        seen.add(vault_id)
    return resolved


def _deployment_agent_ref(value: Any) -> tuple[str, int | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        agent_id = value.get("id") or value.get("agent_id")
        if not agent_id:
            raise HTTPException(status_code=422, detail="Deployment agent reference requires id")
        version = value.get("version")
        return str(agent_id), int(version) if version is not None else None
    raise HTTPException(status_code=422, detail="Deployment agent must be a string or object")


async def _append_deployment_session_events(db: AsyncSession, session, initial_events: list[Any]) -> None:
    _validate_deployment_initial_events(initial_events)
    await validate_user_message_file_references(db, session, initial_events)
    await events_q.append_event(
        db,
        session,
        event_type="session.status_idle",
        payload={"type": "session.status_idle", "status": "idle", "stop_reason": {"type": "end_turn"}},
    )
    for raw_event in initial_events:
        event_type = str(raw_event.get("type") or "")
        payload = dict(raw_event)
        payload["type"] = event_type
        await events_q.append_event(db, session, event_type=event_type, payload=payload)

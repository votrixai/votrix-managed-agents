import re
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db.queries import resources as res_q
from app.models.common import utcnow
from app.models.resources import resource_to_response


MAX_SESSION_FILE_RESOURCES = 100
MAX_SESSION_MEMORY_STORE_RESOURCES = 8


async def create_session_resource(
    db: AsyncSession,
    session,
    data: dict[str, Any],
    *,
    allowed_types: set[str],
):
    if isinstance(data, dict) and data.get("type") == "file":
        await _ensure_file_resource_capacity(db, session)
    if isinstance(data, dict) and data.get("type") == "memory_store":
        await _ensure_memory_store_resource_capacity(db, session)
    normalized = await normalize_session_resource_data(
        db,
        data,
        allowed_types=allowed_types,
        workspace_id=session.workspace_id,
        session_id=session.id,
    )
    return await res_q.create_resource(
        db,
        resource_type="session_resource",
        parent_id=session.id,
        name=session_resource_name(normalized),
        data=normalized,
        workspace_id=session.workspace_id,
    )


async def _ensure_file_resource_capacity(db: AsyncSession, session) -> None:
    resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=session.id,
        limit=1000,
        workspace_id=session.workspace_id,
    )
    file_count = sum(1 for resource in resources if (resource.data or {}).get("type") == "file")
    if file_count >= MAX_SESSION_FILE_RESOURCES:
        raise HTTPException(status_code=422, detail="A session can have at most 100 file resources")


async def _ensure_memory_store_resource_capacity(db: AsyncSession, session) -> None:
    resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=session.id,
        limit=1000,
        workspace_id=session.workspace_id,
    )
    memory_store_count = sum(1 for resource in resources if (resource.data or {}).get("type") == "memory_store")
    if memory_store_count >= MAX_SESSION_MEMORY_STORE_RESOURCES:
        raise HTTPException(status_code=422, detail="A session can have at most 8 memory store resources")


async def normalize_session_resource_data(
    db: AsyncSession,
    data: dict[str, Any],
    *,
    allowed_types: set[str],
    workspace_id: str,
    session_id: str,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Session resource must be an object")
    resource_type = str(data.get("type") or "")
    if resource_type not in allowed_types:
        allowed = ", ".join(sorted(allowed_types))
        raise HTTPException(status_code=422, detail=f"Session resource type must be one of: {allowed}")
    if resource_type == "file":
        return await _normalize_file_session_resource(db, data, workspace_id=workspace_id, session_id=session_id)
    if resource_type == "github_repository":
        return _normalize_github_session_resource(data)
    if resource_type == "memory_store":
        return await _normalize_memory_store_session_resource(db, data, workspace_id=workspace_id)
    raise HTTPException(status_code=422, detail=f"Unsupported session resource type: {resource_type}")


def rotate_session_resource_token(resource, data: dict[str, Any]) -> dict[str, Any]:
    current = dict(resource.data or {})
    if current.get("type") != "github_repository":
        raise HTTPException(status_code=422, detail="Only github_repository session resources support token rotation")
    token = str(data.get("authorization_token") or "")
    if not token:
        raise HTTPException(status_code=422, detail="authorization_token is required")
    current["authorization_token_present"] = True
    current["authorization_token_updated_at"] = utcnow().isoformat()
    current.pop("authorization_token", None)
    return current


async def delete_session_resource_file(db: AsyncSession, resource) -> None:
    data = dict(resource.data or {})
    if data.get("type") != "file":
        return
    file_id = data.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return
    scoped_file = await res_q.get_resource(
        db,
        resource_id=file_id,
        resource_type="file",
        workspace_id=resource.workspace_id,
    )
    if scoped_file is None:
        return
    scope = (scoped_file.data or {}).get("scope")
    if not isinstance(scope, dict) or scope.get("type") != "session" or scope.get("id") != resource.parent_id:
        return
    if storage.is_object_storage_backend(scoped_file.storage_backend) and scoped_file.storage_key:
        active_references = await res_q.count_resources_by_storage_key(
            db,
            resource_type="file",
            storage_backend=scoped_file.storage_backend,
            storage_key=scoped_file.storage_key,
            workspace_id=resource.workspace_id,
        )
        if active_references <= 1:
            await storage.delete_file(scoped_file.storage_key)
    await res_q.delete_resource(db, scoped_file)


def ensure_session_resource_deletable(resource) -> None:
    if (resource.data or {}).get("type") == "memory_store":
        raise HTTPException(status_code=422, detail="memory_store session resources cannot be removed after creation")


async def session_resources_response(db: AsyncSession, session) -> list[dict[str, Any]]:
    resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=session.id,
        limit=1000,
        workspace_id=session.workspace_id,
    )
    if resources:
        return [session_resource_response(resource) for resource in resources]
    return list((session.status_details or {}).get("resources") or [])


async def session_has_memory_store(db: AsyncSession, session, memory_store_id: str) -> bool:
    resources = await res_q.list_resources(
        db,
        resource_type="session_resource",
        parent_id=session.id,
        limit=1000,
        workspace_id=session.workspace_id,
    )
    if any((resource.data or {}).get("memory_store_id") == memory_store_id for resource in resources):
        return True
    return any(
        resource.get("memory_store_id") == memory_store_id
        for resource in (session.status_details or {}).get("resources", [])
        if isinstance(resource, dict)
    )


def session_resource_response(resource) -> dict[str, Any]:
    data = dict(resource.data or {})
    if data.get("type") == "file" or data.get("file_id"):
        file_id = str(data.get("file_id") or "")
        return {
            "id": resource.id,
            "type": "file",
            "file_id": file_id,
            "mount_path": data.get("mount_path") or f"/mnt/session/uploads/{file_id}",
            "created_at": resource.created_at,
            "updated_at": resource.updated_at,
        }
    if data.get("type") == "github_repository":
        response = {
            "id": resource.id,
            "type": "github_repository",
            "url": str(data.get("url") or ""),
            "mount_path": str(data.get("mount_path") or _default_github_mount_path(str(data.get("url") or ""))),
            "created_at": resource.created_at,
            "updated_at": resource.updated_at,
        }
        if data.get("checkout") is not None:
            response["checkout"] = data["checkout"]
        return response
    if data.get("type") == "memory_store":
        response = {
            "id": resource.id,
            "type": "memory_store",
            "memory_store_id": str(data.get("memory_store_id") or ""),
            "access": data.get("access") or "read_write",
            "description": str(data.get("description") or ""),
            "mount_path": data.get("mount_path"),
            "name": data.get("name"),
            "created_at": resource.created_at,
            "updated_at": resource.updated_at,
        }
        if data.get("instructions") is not None:
            response["instructions"] = data["instructions"]
        return {key: value for key, value in response.items() if value is not None}
    response = resource_to_response(resource, public_type="session_resource")
    response.pop("authorization_token", None)
    return response


def session_resource_name(data: dict[str, Any]) -> str | None:
    if data.get("type") == "file":
        return data.get("file_id")
    if data.get("type") == "github_repository":
        return _github_repo_name(str(data.get("url") or ""))
    if data.get("type") == "memory_store":
        return data.get("name") or data.get("memory_store_id")
    return data.get("name")


async def _normalize_file_session_resource(
    db: AsyncSession,
    data: dict[str, Any],
    *,
    workspace_id: str,
    session_id: str,
) -> dict[str, Any]:
    file_id = str(data.get("file_id") or "")
    if not file_id:
        raise HTTPException(status_code=422, detail="file session resources require file_id")
    file_resource = await res_q.get_resource(db, resource_id=file_id, resource_type="file", workspace_id=workspace_id)
    if file_resource is None:
        raise HTTPException(status_code=404, detail="File not found")
    mount_path = str(data.get("mount_path") or f"/mnt/session/uploads/{file_id}")
    _validate_mount_path(mount_path)
    copied = await _copy_file_for_session_resource(file_resource, session_id=session_id, workspace_id=workspace_id)
    if copied is None:
        raise HTTPException(status_code=500, detail="File object is not stored in object storage")
    session_file = await _create_session_scoped_file(
        db,
        source_file=file_resource,
        copied=copied,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    normalized = {
        "type": "file",
        "file_id": session_file.id,
        "source_file_id": file_id,
        "mount_path": mount_path,
        "read_only": True,
        "session_file": copied,
    }
    return normalized


async def _copy_file_for_session_resource(file_resource, *, session_id: str, workspace_id: str) -> dict[str, Any] | None:
    if not (storage.is_object_storage_backend(file_resource.storage_backend) and file_resource.storage_key):
        return None
    filename = file_resource.filename or file_resource.name or file_resource.id
    destination_key = storage.object_key(
        namespace=f"sessions_{session_id}",
        category="resources",
        filename=filename,
        content_sha256=file_resource.sha256,
        workspace_id=workspace_id,
    )
    try:
        await storage.copy_file(
            file_resource.storage_key,
            destination_key,
            content_type=file_resource.content_type,
        )
    except storage.StorageConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "source_file_id": file_resource.id,
        "filename": filename,
        "mime_type": file_resource.content_type or "application/octet-stream",
        "size_bytes": file_resource.size_bytes,
        "sha256": file_resource.sha256,
        "storage": {
            "backend": file_resource.storage_backend,
            "key": destination_key,
            "url": storage.public_url_for_key(destination_key),
        },
    }


async def _create_session_scoped_file(
    db: AsyncSession,
    *,
    source_file,
    copied: dict[str, Any],
    session_id: str,
    workspace_id: str,
):
    storage_data = copied["storage"]
    return await res_q.create_resource(
        db,
        resource_type="file",
        name=copied["filename"],
        filename=copied["filename"],
        content_type=copied["mime_type"],
        data={
            "filename": copied["filename"],
            "mime_type": copied["mime_type"],
            "source_file_id": source_file.id,
            "scope": {"type": "session", "id": session_id},
        },
        storage_backend=storage_data["backend"],
        storage_key=storage_data["key"],
        storage_url=storage_data["url"],
        size_bytes=copied.get("size_bytes"),
        sha256=copied.get("sha256"),
        workspace_id=workspace_id,
    )


def _normalize_github_session_resource(data: dict[str, Any]) -> dict[str, Any]:
    url = str(data.get("url") or "")
    if not url:
        raise HTTPException(status_code=422, detail="github_repository session resources require url")
    token = str(data.get("authorization_token") or "")
    if not token:
        raise HTTPException(status_code=422, detail="github_repository session resources require authorization_token")
    mount_path = str(data.get("mount_path") or _default_github_mount_path(url))
    _validate_mount_path(mount_path)
    normalized: dict[str, Any] = {
        "type": "github_repository",
        "url": url,
        "mount_path": mount_path,
        "authorization_token_present": True,
        "authorization_token_updated_at": utcnow().isoformat(),
    }
    if data.get("checkout") is not None:
        normalized["checkout"] = _normalize_github_checkout(data.get("checkout"))
    return normalized


async def _normalize_memory_store_session_resource(
    db: AsyncSession,
    data: dict[str, Any],
    *,
    workspace_id: str,
) -> dict[str, Any]:
    memory_store_id = str(data.get("memory_store_id") or "")
    if not memory_store_id:
        raise HTTPException(status_code=422, detail="memory_store session resources require memory_store_id")
    store = await res_q.get_resource(db, resource_id=memory_store_id, resource_type="memory_store", workspace_id=workspace_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Memory store not found")
    if store.archived_at is not None:
        raise HTTPException(status_code=404, detail="Memory store not found")
    access = str(data.get("access") or "read_write")
    if access not in {"read_write", "read_only"}:
        raise HTTPException(status_code=422, detail="memory_store access must be read_write or read_only")
    store_data = dict(store.data or {})
    name = str(store.name or store_data.get("name") or memory_store_id)
    normalized: dict[str, Any] = {
        "type": "memory_store",
        "memory_store_id": memory_store_id,
        "access": access,
        "name": name,
        "description": str(store_data.get("description") or ""),
        "mount_path": _memory_store_mount_path(name, memory_store_id),
    }
    if data.get("instructions") is not None:
        instructions = str(data.get("instructions") or "")
        if len(instructions) > 4096:
            raise HTTPException(status_code=422, detail="memory_store instructions must be 4096 characters or fewer")
        normalized["instructions"] = instructions
    return normalized


def _normalize_github_checkout(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="github_repository checkout must be an object")
    checkout_type = str(value.get("type") or "")
    if checkout_type == "branch":
        name = str(value.get("name") or "")
        if not name:
            raise HTTPException(status_code=422, detail="branch checkout requires name")
        return {"type": "branch", "name": name}
    if checkout_type == "commit":
        sha = str(value.get("sha") or "")
        if not sha:
            raise HTTPException(status_code=422, detail="commit checkout requires sha")
        return {"type": "commit", "sha": sha}
    raise HTTPException(status_code=422, detail="checkout type must be branch or commit")


def _default_github_mount_path(url: str) -> str:
    repo_name = _github_repo_name(url) or "repository"
    return f"/workspace/{repo_name}"


def _github_repo_name(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or url
    name = path.rstrip("/").split("/")[-1] or "repository"
    if name.endswith(".git"):
        name = name[:-4]
    return _safe_mount_segment(name) or "repository"


def _memory_store_mount_path(name: str, fallback: str) -> str:
    segment = _safe_mount_segment(name) or _safe_mount_segment(fallback) or "memory"
    return f"/mnt/memory/{segment}"


def _safe_mount_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-").lower()


def _validate_mount_path(value: str) -> None:
    if not value.startswith("/"):
        raise HTTPException(status_code=422, detail="mount_path must be absolute")

import hashlib
import io
import json
import zipfile
from time import time_ns
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_access
from app.config import get_settings
from app.content_scan import UnsafeContentError, validate_upload_content
from app.db.engine import get_session
from app.db.queries import resources as res_q
from app.models.common import ListResponse
from app.models.resources import resource_to_response
from app.pagination import paginate, sort_by_created_at
from app.storage import (
    StorageConfigurationError,
    download_file_with_type,
    is_object_storage_backend,
    save_file_bytes,
    should_store_in_object_storage,
)

SKILL_SOURCES = {"anthropic", "custom"}

router = APIRouter(
    prefix="/v1/skills",
    tags=["skills"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", status_code=201)
async def create_skill(request: Request, db: AsyncSession = Depends(get_session)):
    data, content = await _skill_payload_from_request(request)
    version_number = _new_skill_version_id()
    skill = await res_q.create_resource(
        db,
        resource_type="skill",
        name=data.get("display_title") or data.get("name"),
        data={
            "display_title": data.get("display_title"),
            "name": data.get("name"),
            "description": data.get("description"),
            "top_level_directory": data.get("top_level_directory"),
            "latest_version": version_number,
        },
    )
    version = await _create_skill_version_resource(db, skill.id, version_number, data, content)
    await db.commit()
    response = _skill_response(skill)
    response["version"] = _skill_version_response(version)
    return response


@router.get("")
async def list_skills(
    limit: int = 50,
    page: str | None = None,
    source: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    if source is not None and source not in SKILL_SOURCES:
        raise HTTPException(status_code=422, detail="source must be one of: anthropic, custom")

    skills = await res_q.list_resources(db, resource_type="skill", limit=1000)
    skills = sort_by_created_at(skills, order="desc")
    responses = [_skill_response(skill) for skill in skills]
    if source is not None:
        responses = [skill for skill in responses if skill.get("source") == source]
    return paginate(responses, limit=limit, page=page)


@router.get("/{skill_id}")
async def retrieve_skill(skill_id: str, db: AsyncSession = Depends(get_session)):
    skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return _skill_response(skill)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, db: AsyncSession = Depends(get_session)):
    skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    await res_q.delete_resource(db, skill)
    await db.commit()
    return {"id": skill.id, "type": "skill_deleted", "deleted": True}


@router.post("/{skill_id}/versions", status_code=201)
async def create_skill_version(
    skill_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    data, content = await _skill_payload_from_request(request)
    version_number = _new_skill_version_id(skill.data.get("latest_version"))
    version = await _create_skill_version_resource(db, skill.id, version_number, data, content)
    skill_data = dict(skill.data)
    skill_data["latest_version"] = version_number
    await res_q.update_resource(db, skill, data=skill_data)
    await db.commit()
    return _skill_version_response(version)


@router.get("/{skill_id}/versions")
async def list_skill_versions(
    skill_id: str,
    limit: int = 50,
    page: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    versions = await res_q.list_resources(db, resource_type="skill_version", parent_id=skill_id, limit=1000)
    versions = sort_by_created_at(versions, order="desc")
    return paginate([_skill_version_response(v) for v in versions], limit=limit, page=page)


@router.get("/{skill_id}/versions/{version}")
async def retrieve_skill_version(
    skill_id: str,
    version: int,
    db: AsyncSession = Depends(get_session),
):
    skill_version = await res_q.get_resource_version(
        db,
        resource_type="skill_version",
        parent_id=skill_id,
        version=version,
    )
    if skill_version is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    return _skill_version_response(skill_version)


@router.delete("/{skill_id}/versions/{version}")
async def delete_skill_version(skill_id: str, version: int, db: AsyncSession = Depends(get_session)):
    skill_version = await res_q.get_resource_version(
        db,
        resource_type="skill_version",
        parent_id=skill_id,
        version=version,
    )
    if skill_version is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    await res_q.delete_resource(db, skill_version)
    await db.commit()
    return {"id": str(skill_version.version), "type": "skill_version_deleted", "deleted": True}


@router.get("/{skill_id}/versions/{version}/content")
async def download_skill_version(skill_id: str, version: int, db: AsyncSession = Depends(get_session)):
    skill_version = await res_q.get_resource_version(
        db,
        resource_type="skill_version",
        parent_id=skill_id,
        version=version,
    )
    if skill_version is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    if not (is_object_storage_backend(skill_version.storage_backend) and skill_version.storage_key):
        raise HTTPException(status_code=500, detail="Skill version object is not stored in object storage")
    content, stored_content_type = await download_file_with_type(skill_version.storage_key)
    content_type = stored_content_type or skill_version.content_type or "application/zip"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{skill_version.filename or skill_version.id}.zip"'},
    )


async def _create_skill_version_resource(
    db: AsyncSession,
    skill_id: str,
    version_number: int,
    data: dict[str, Any],
    content: bytes,
):
    _enforce_skill_archive_size(content)
    _scan_skill_content(content, label="Skill archive")
    sha256 = hashlib.sha256(content).hexdigest()
    try:
        should_store_in_object_storage()
        stored = await save_file_bytes(
            content,
            "application/zip",
            namespace=f"skills/{skill_id}",
            filename=f"skill-v{version_number}-{sha256}.zip",
            category="versions",
        )
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return await res_q.create_resource(
        db,
        resource_type="skill_version",
        parent_id=skill_id,
        version=version_number,
        content=None,
        content_type="application/zip",
        filename=f"skill-v{version_number}.zip",
        data={
            "version": version_number,
            "files": data.get("files", []),
            "archive_format": "zip",
            "name": data.get("name"),
            "description": data.get("description"),
            "top_level_directory": data.get("top_level_directory"),
            "manifest": data.get("manifest"),
        },
        storage_backend=stored.backend,
        storage_key=stored.key,
        storage_url=stored.url,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256 or sha256,
    )


def _skill_response(skill) -> dict[str, Any]:
    response = resource_to_response(skill, public_type="skill")
    latest_version = response.get("latest_version")
    response["latest_version"] = str(latest_version) if latest_version is not None else None
    response["source"] = response.get("source") or "custom"
    response["display_title"] = response.get("display_title")
    return response


def _skill_version_response(skill_version) -> dict[str, Any]:
    response = resource_to_response(skill_version, public_type="skill_version")
    version = response.get("version")
    response["version"] = str(version) if version is not None else ""
    response["skill_id"] = skill_version.parent_id
    response["directory"] = response.get("directory") or response.get("top_level_directory") or ""
    response["name"] = response.get("name") or ""
    response["description"] = response.get("description") or ""
    return response


async def _skill_payload_from_request(request: Request) -> tuple[dict[str, Any], bytes]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        display_title = form.get("display_title")
        uploaded_files = []
        file_records = []
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                raw = await value.read()
                filename = _normalize_zip_path(getattr(value, "filename", key))
                _scan_skill_content(raw, label=f"Skill file {filename}")
                uploaded_files.append((filename, raw, getattr(value, "content_type", None)))
                file_records.append(
                    {
                        "filename": filename,
                        "mime_type": getattr(value, "content_type", None),
                        "size_bytes": len(raw),
                    }
                )
        manifest = _validate_skill_files(uploaded_files) if uploaded_files else {}
        content = _zip_uploaded_files(uploaded_files)
        return {
            "display_title": display_title,
            "name": manifest.get("name"),
            "description": manifest.get("description"),
            "files": file_records,
            "top_level_directory": manifest.get("top_level_directory"),
            "manifest": manifest,
        }, content

    body = await request.json()
    files = body.get("files")
    if isinstance(files, list) and files:
        uploaded_files = []
        file_records = []
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = _normalize_zip_path(str(item.get("filename") or item.get("path") or "file"))
            raw_value = item.get("content", "")
            raw = raw_value.encode("utf-8") if isinstance(raw_value, str) else bytes(raw_value)
            mime_type = item.get("mime_type")
            _scan_skill_content(raw, label=f"Skill file {filename}")
            uploaded_files.append((filename, raw, mime_type))
            file_records.append({"filename": filename, "mime_type": mime_type, "size_bytes": len(raw)})
        manifest = _validate_skill_files(uploaded_files)
        return {
            **body,
            "name": body.get("name") or manifest.get("name"),
            "description": body.get("description") or manifest.get("description"),
            "files": file_records,
            "top_level_directory": manifest.get("top_level_directory"),
            "manifest": manifest,
        }, _zip_uploaded_files(uploaded_files)

    manifest = json.dumps(body, separators=(",", ":")).encode("utf-8")
    content = _zip_uploaded_files([("manifest.json", manifest, "application/json")])
    return body, content


def _zip_uploaded_files(uploaded_files: list[tuple[str, bytes, str | None]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for filename, raw, _mime_type in uploaded_files:
            archive.writestr(_normalize_zip_path(filename), raw)
    return buffer.getvalue()


def _enforce_skill_archive_size(content: bytes) -> None:
    max_bytes = get_settings().vma_max_skill_archive_bytes
    if max_bytes > 0 and len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Skill archive exceeds maximum size of {max_bytes} bytes",
        )


def _scan_skill_content(content: bytes, *, label: str) -> None:
    try:
        validate_upload_content(content, label=label)
    except UnsafeContentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _new_skill_version_id(previous: Any = None) -> int:
    candidate = time_ns() // 1_000
    try:
        previous_version = int(previous or 0)
    except (TypeError, ValueError):
        previous_version = 0
    return max(candidate, previous_version + 1)


def _normalize_zip_path(filename: str) -> str:
    raw = filename.replace("\\", "/")
    if raw.startswith("/"):
        raise HTTPException(status_code=422, detail="Skill file paths must be relative")
    parts = raw.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Skill file paths must not contain empty, . or .. segments")
    return "/".join(parts) or "file"


def _validate_skill_files(uploaded_files: list[tuple[str, bytes, str | None]]) -> dict[str, Any]:
    if not uploaded_files:
        raise HTTPException(status_code=422, detail="Skill uploads must include files")

    normalized_paths = [_normalize_zip_path(filename) for filename, _raw, _mime_type in uploaded_files]
    path_parts = [path.split("/") for path in normalized_paths]
    if any(len(parts) < 2 for parts in path_parts):
        raise HTTPException(status_code=422, detail="Skill files must live under one top-level directory")

    top_level = path_parts[0][0]
    if any(parts[0] != top_level for parts in path_parts):
        raise HTTPException(status_code=422, detail="Skill files must share one top-level directory")

    skill_path = f"{top_level}/SKILL.md"
    skill_file = next(
        (raw for filename, raw, _mime_type in uploaded_files if _normalize_zip_path(filename) == skill_path),
        None,
    )
    if skill_file is None:
        raise HTTPException(status_code=422, detail="Skill uploads must include root SKILL.md")

    frontmatter = _parse_skill_frontmatter(skill_file)
    missing = [field for field in ("name", "description") if not frontmatter.get(field)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"SKILL.md frontmatter is missing required field(s): {', '.join(missing)}",
        )
    return {**frontmatter, "top_level_directory": top_level}


def _parse_skill_frontmatter(raw: bytes) -> dict[str, str]:
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HTTPException(status_code=422, detail="SKILL.md must start with YAML frontmatter")
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise HTTPException(status_code=422, detail="SKILL.md frontmatter must be closed with ---")

    frontmatter: dict[str, str] = {}
    for line in lines[1:end_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise HTTPException(status_code=422, detail="SKILL.md frontmatter must use key: value lines")
        key, value = stripped.split(":", 1)
        clean_value = value.strip().strip('"').strip("'")
        frontmatter[key.strip()] = clean_value
    return frontmatter

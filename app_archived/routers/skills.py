import hashlib
import io
import json
import mimetypes
import stat
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
from app.governance_runtime import governance_service
from app.models.common import ListResponse
from app.models.resources import resource_to_response
from app.models.skills import (
    SkillDeletedResponse,
    SkillResponse,
    SkillVersionDeletedResponse,
    SkillVersionResponse,
)
from app.pagination import paginate, sort_by_created_at
from app.storage import (
    StorageConfigurationError,
    download_file_with_type,
    is_object_storage_backend,
    save_file_bytes,
    should_store_in_object_storage,
)
from app.organization import resolve_organization_id

SKILL_SOURCES = {"anthropic", "custom"}
MAX_SKILL_ZIP_MEMBERS = 1_000
MAX_SKILL_ZIP_PATH_BYTES = 4_096
MAX_SKILL_ZIP_PATH_SEGMENT_BYTES = 255
MAX_SKILL_ZIP_COMPRESSION_RATIO = 1_000
ABSOLUTE_MAX_SKILL_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
SUPPORTED_SKILL_ZIP_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

router = APIRouter(
    prefix="/v1/skills",
    tags=["skills"],
    dependencies=[Depends(require_api_access)],
)


@router.post("", response_model=SkillResponse, status_code=201)
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


@router.get("", response_model=ListResponse[SkillResponse])
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


@router.get("/{skill_id}", response_model=SkillResponse)
async def retrieve_skill(skill_id: str, db: AsyncSession = Depends(get_session)):
    skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return _skill_response(skill)


@router.delete("/{skill_id}", response_model=SkillDeletedResponse)
async def delete_skill(skill_id: str, db: AsyncSession = Depends(get_session)):
    skill = await res_q.get_resource(db, resource_id=skill_id, resource_type="skill")
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    await res_q.delete_resource(db, skill)
    await db.commit()
    return {"id": skill.id, "type": "skill_deleted", "deleted": True}


@router.post(
    "/{skill_id}/versions",
    response_model=SkillVersionResponse,
    status_code=201,
)
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


@router.get(
    "/{skill_id}/versions",
    response_model=ListResponse[SkillVersionResponse],
)
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


@router.get(
    "/{skill_id}/versions/{version}",
    response_model=SkillVersionResponse,
)
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


@router.delete(
    "/{skill_id}/versions/{version}",
    response_model=SkillVersionDeletedResponse,
)
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
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{skill_version.filename or skill_version.id}.zip"',
            "X-Content-Type-Options": "nosniff",
        },
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
    if get_settings().vma_governance_enabled:
        await governance_service().enforce_storage_quota(
            db,
            resolve_organization_id(),
            len(content),
        )
    try:
        should_store_in_object_storage()
        stored = await save_file_bytes(
            content,
            "application/zip",
            namespace=f"skills/{skill_id}",
            filename=f"skill-v{version_number}-{sha256}.zip",
            category="versions",
            organization_id=resolve_organization_id(),
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
        storage_url=None,
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
        multipart_files = []
        multipart_bytes = 0
        multipart_byte_limit = _skill_archive_byte_limit()
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                filename = _normalize_zip_path(getattr(value, "filename", key))
                raw = await _read_multipart_upload(
                    value,
                    max_bytes=multipart_byte_limit - multipart_bytes,
                    label="Skill multipart upload",
                )
                multipart_bytes += len(raw)
                multipart_files.append((filename, raw, getattr(value, "content_type", None)))

        zip_uploads = [item for item in multipart_files if item[0].lower() == "skill.zip"]
        if zip_uploads and len(multipart_files) != 1:
            raise HTTPException(
                status_code=422,
                detail="A skill.zip archive must be the only multipart file",
            )
        if len(zip_uploads) == 1:
            _scan_skill_content(multipart_files[0][1], label="Skill ZIP archive")
            uploaded_files = _unpack_skill_zip(multipart_files[0][1])
        else:
            uploaded_files = multipart_files
            for filename, raw, _mime_type in uploaded_files:
                _scan_skill_content(raw, label=f"Skill file {filename}")

        file_records = [
            {
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(raw),
            }
            for filename, raw, mime_type in uploaded_files
        ]
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


async def _read_multipart_upload(
    value: Any,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    if max_bytes < 0:
        raise HTTPException(status_code=413, detail=f"{label} exceeds maximum size")
    raw = await value.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds maximum size of {_skill_archive_byte_limit()} bytes",
        )
    return raw


def _unpack_skill_zip(content: bytes) -> list[tuple[str, bytes, str | None]]:
    byte_limit = _skill_archive_byte_limit()
    if len(content) > byte_limit:
        raise HTTPException(
            status_code=413,
            detail=f"Skill ZIP archive exceeds maximum size of {byte_limit} bytes",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(status_code=422, detail="Uploaded skill.zip is not a valid ZIP archive") from exc

    try:
        with archive:
            members = archive.infolist()
            if len(members) > MAX_SKILL_ZIP_MEMBERS:
                raise HTTPException(
                    status_code=413,
                    detail=f"Skill ZIP archive exceeds maximum member count of {MAX_SKILL_ZIP_MEMBERS}",
                )

            uploaded_files: list[tuple[str, bytes, str | None]] = []
            member_paths: set[str] = set()
            directory_paths: set[str] = set()
            file_paths: set[str] = set()
            top_level_directories: set[str] = set()
            declared_size = 0
            extracted_size = 0

            for member in members:
                normalized, is_directory = _validate_skill_zip_member(member)
                if normalized in member_paths:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Skill ZIP archive contains duplicate path: {normalized}",
                    )
                member_paths.add(normalized)
                top_level_directories.add(normalized.split("/", 1)[0])

                if is_directory:
                    if normalized in file_paths:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Skill ZIP path is both a file and directory: {normalized}",
                        )
                    directory_paths.add(normalized)
                    continue

                if normalized in directory_paths:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Skill ZIP path is both a file and directory: {normalized}",
                    )
                file_paths.add(normalized)
                declared_size += member.file_size
                if declared_size > byte_limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Skill ZIP expanded content exceeds maximum size of {byte_limit} bytes",
                    )
                if _skill_zip_compression_ratio(member) > MAX_SKILL_ZIP_COMPRESSION_RATIO:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Skill ZIP member has an unsafe compression ratio: {normalized}",
                    )

                remaining = byte_limit - extracted_size
                raw = _read_skill_zip_member(archive, member, remaining=remaining)
                extracted_size += len(raw)
                _scan_skill_content(raw, label=f"Skill file {normalized}")
                uploaded_files.append((normalized, raw, mimetypes.guess_type(normalized)[0]))

            if len(top_level_directories) > 1:
                raise HTTPException(
                    status_code=422,
                    detail="Skill files must share one top-level directory",
                )
            _reject_skill_zip_file_directory_conflicts(file_paths)
            return uploaded_files
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Uploaded skill.zip could not be safely read") from exc


def _validate_skill_zip_member(member: zipfile.ZipInfo) -> tuple[str, bool]:
    if member.flag_bits & 0x1:
        raise HTTPException(status_code=422, detail="Encrypted Skill ZIP members are not supported")
    if member.compress_type not in SUPPORTED_SKILL_ZIP_COMPRESSION:
        raise HTTPException(status_code=422, detail="Skill ZIP uses an unsupported compression method")

    raw_name = member.filename.replace("\\", "/")
    is_directory = member.is_dir() or raw_name.endswith("/")
    candidate = raw_name[:-1] if is_directory else raw_name
    normalized = _normalize_zip_path(candidate)
    if len(normalized.encode("utf-8")) > MAX_SKILL_ZIP_PATH_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Skill ZIP member path exceeds {MAX_SKILL_ZIP_PATH_BYTES} bytes",
        )

    unix_mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if is_directory:
        if file_type not in {0, stat.S_IFDIR}:
            raise HTTPException(status_code=422, detail=f"Skill ZIP contains a special file: {normalized}")
    elif file_type not in {0, stat.S_IFREG}:
        kind = "symbolic link" if file_type == stat.S_IFLNK else "special file"
        raise HTTPException(status_code=422, detail=f"Skill ZIP contains a {kind}: {normalized}")
    return normalized, is_directory


def _skill_zip_compression_ratio(member: zipfile.ZipInfo) -> float:
    if member.file_size <= 0:
        return 1.0
    if member.compress_size <= 0:
        return float("inf")
    return member.file_size / member.compress_size


def _read_skill_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, *, remaining: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    with archive.open(member, mode="r") as source:
        while True:
            chunk = source.read(min(64 * 1024, remaining - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > remaining:
                raise HTTPException(
                    status_code=413,
                    detail=f"Skill ZIP expanded content exceeds maximum size of {_skill_archive_byte_limit()} bytes",
                )
            chunks.append(chunk)
    raw = b"".join(chunks)
    if len(raw) != member.file_size:
        raise HTTPException(status_code=422, detail=f"Skill ZIP member size is invalid: {member.filename}")
    return raw


def _reject_skill_zip_file_directory_conflicts(file_paths: set[str]) -> None:
    for path in file_paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in file_paths:
                raise HTTPException(
                    status_code=422,
                    detail=f"Skill ZIP file path conflicts with a directory: {parent}",
                )


def _skill_archive_byte_limit() -> int:
    configured = int(get_settings().vma_max_skill_archive_bytes)
    if configured > 0:
        return min(configured, ABSOLUTE_MAX_SKILL_ZIP_EXPANDED_BYTES)
    return ABSOLUTE_MAX_SKILL_ZIP_EXPANDED_BYTES


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
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise HTTPException(status_code=422, detail="Skill file paths must not contain control characters")
    if raw.startswith("/"):
        raise HTTPException(status_code=422, detail="Skill file paths must be relative")
    parts = raw.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Skill file paths must not contain empty, . or .. segments")
    if len(parts[0]) == 2 and parts[0][0].isalpha() and parts[0][1] == ":":
        raise HTTPException(status_code=422, detail="Skill file paths must not use Windows drive prefixes")
    if any(len(part.encode("utf-8")) > MAX_SKILL_ZIP_PATH_SEGMENT_BYTES for part in parts):
        raise HTTPException(
            status_code=422,
            detail=f"Skill file path segments must not exceed {MAX_SKILL_ZIP_PATH_SEGMENT_BYTES} bytes",
        )
    if len(raw.encode("utf-8")) > MAX_SKILL_ZIP_PATH_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Skill file paths must not exceed {MAX_SKILL_ZIP_PATH_BYTES} bytes",
        )
    return "/".join(parts) or "file"


def _validate_skill_files(uploaded_files: list[tuple[str, bytes, str | None]]) -> dict[str, Any]:
    if not uploaded_files:
        raise HTTPException(status_code=422, detail="Skill uploads must include files")

    normalized_paths = [_normalize_zip_path(filename) for filename, _raw, _mime_type in uploaded_files]
    if len(normalized_paths) != len(set(normalized_paths)):
        raise HTTPException(status_code=422, detail="Skill uploads must not contain duplicate paths")
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

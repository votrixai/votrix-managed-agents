from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

from app.db.models import Skill
from app.db.queries import DEFAULT_PAGE_SIZE
from app.models.common import DeletedResponse, ListResponse, page_of
from app.models.skills import SkillResponse
from app.routers.deps import Db, OrganizationId
from app.services import skills as service

router = APIRouter(prefix="/v1/skills", tags=["skills"])


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    db: Db,
    organization_id: OrganizationId,
    file: UploadFile = File(..., description="The skill package, as a zip"),
    description: str | None = Form(default=None),
):
    """Upload a skill package.

    Multipart rather than a presigned URL: the package is unpacked inside a
    sandbox and read by the agent as instructions, so it gets checked on the
    way in. Its own SKILL.md is what names it.
    """
    try:
        skill = await service.create_skill(
            db,
            organization_id=organization_id,
            content=await file.read(),
            description=description,
        )
    except service.InvalidSkillPackage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return to_skill(skill)


@router.get("", response_model=ListResponse[SkillResponse])
async def list_skills(
    db: Db,
    organization_id: OrganizationId,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
):
    skills = await service.list_skills(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return page_of(skills, to_skill)


@router.get("/{skill_id}", response_model=SkillResponse)
async def retrieve_skill(skill_id: str, db: Db, organization_id: OrganizationId):
    skill = await service.get_skill(db, skill_id=skill_id, organization_id=organization_id)
    return to_skill(skill)


@router.post("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    db: Db,
    organization_id: OrganizationId,
    file: UploadFile | None = File(
        default=None, description="A replacement package, as a zip"
    ),
    name: str | None = Form(default=None),
    description: str | None = Form(default=None),
):
    """Edit a skill, optionally replacing what is in it.

    Multipart, like create, because the package can come through here too. The
    id does not change, so every Agent already referencing this skill picks up
    the new contents without being edited — which is the reason this exists
    rather than callers deleting and re-uploading.

    Sending a package re-derives the name from its SKILL.md and ignores `name`:
    a package that disagreed with the name stored beside it would fail to load
    in the sandbox. Sessions already running keep the package their container
    was built with.
    """
    try:
        skill = await service.update_skill(
            db,
            skill_id=skill_id,
            organization_id=organization_id,
            content=await file.read() if file is not None else None,
            name=name,
            description=description,
        )
    except service.InvalidSkillPackage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return to_skill(skill)


@router.get("/{skill_id}/content")
async def download_skill(skill_id: str, db: Db, organization_id: OrganizationId) -> Response:
    content, content_type = await service.download_skill(
        db, skill_id=skill_id, organization_id=organization_id
    )
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{skill_id}.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{skill_id}", response_model=DeletedResponse)
async def delete_skill(skill_id: str, db: Db, organization_id: OrganizationId):
    skill = await service.delete_skill(db, skill_id=skill_id, organization_id=organization_id)
    return DeletedResponse(id=skill.id)


def to_skill(skill: Skill) -> SkillResponse:
    """`storage_key` is not listed, and that is the point — where the package
    lives in object storage never leaves this service."""
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        size_bytes=skill.size_bytes,
        sha256=skill.sha256,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        archived_at=skill.archived_at,
    )

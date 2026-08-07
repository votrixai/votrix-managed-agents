"""Uploading a skill and getting it back.

Object storage is swapped for a dict — this is about the API and the package
checks, not about R2 being reachable.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
import pytest_asyncio
from e2b import CommandExitException
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.deps import get_db
from app.services import skills as service

SKILL_MD = b"""---
name: pptx
description: Build PowerPoint decks
---

# pptx

Use `python-pptx`.
"""


def make_zip(files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in (files or {"SKILL.md": SKILL_MD, "scripts/run.py": b"print(1)\n"}).items():
            archive.writestr(path, body)
    return buffer.getvalue()


@pytest_asyncio.fixture
async def bucket(monkeypatch) -> dict[str, bytes]:
    """Object storage, in a dict."""
    store: dict[str, bytes] = {}

    async def save_bytes(data, *, organization_id, category, filename, mime_type=None):
        key = f"organizations/{organization_id}/{category}/{filename}"
        store[key] = data
        return service.storage.StoredObject(
            key=key,
            content_type=mime_type or "application/zip",
            size_bytes=len(data),
            sha256=service.sha256(data),
        )

    async def download_bytes(key):
        return store[key], "application/zip"

    async def delete_object(key):
        store.pop(key, None)

    monkeypatch.setattr(service.storage, "save_bytes", save_bytes)
    monkeypatch.setattr(service.storage, "download_bytes", download_bytes)
    monkeypatch.setattr(service.storage, "delete_object", delete_object)
    return store


@pytest_asyncio.fixture
async def client(db, bucket):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def upload(client, headers, content=None, **form):
    return await client.post(
        "/v1/skills",
        headers=headers,
        files={"file": ("pptx.zip", content if content is not None else make_zip(), "application/zip")},
        data=form,
    )


# --- the round trip ----------------------------------------------------------


async def test_a_skill_can_be_uploaded_and_downloaded_again(client, headers):
    created = await upload(client, headers)
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]

    downloaded = await client.get(f"/v1/skills/{skill_id}/content", headers=headers)

    assert downloaded.status_code == 200
    inside = zipfile.ZipFile(io.BytesIO(downloaded.content))
    assert inside.read("pptx/SKILL.md") == SKILL_MD
    assert inside.read("pptx/scripts/run.py") == b"print(1)\n"


async def test_the_stored_package_always_carries_its_own_directory(client, headers):
    """One shape in storage means the sandbox has one unpacking rule.

    Uploads arrive both ways — SKILL.md at the root, or already inside its
    directory — and both have to end up as `<name>/SKILL.md`.
    """
    async def stored_names(zip_bytes):
        skill_id = (await upload(client, headers, zip_bytes)).json()["id"]
        content = (await client.get(f"/v1/skills/{skill_id}/content", headers=headers)).content
        await client.delete(f"/v1/skills/{skill_id}", headers=headers)
        return zipfile.ZipFile(io.BytesIO(content)).namelist()

    flat = await stored_names(make_zip({"SKILL.md": SKILL_MD}))
    nested = await stored_names(make_zip({"pptx/SKILL.md": SKILL_MD}))

    assert flat == ["pptx/SKILL.md"]
    assert nested == ["pptx/SKILL.md"]


async def test_the_download_comes_back_as_an_attachment(client, headers):
    skill_id = (await upload(client, headers)).json()["id"]

    response = await client.get(f"/v1/skills/{skill_id}/content", headers=headers)

    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]


async def test_the_package_names_itself(client, headers):
    """SKILL.md is the source of truth, not whatever the client claims."""
    body = (await upload(client, headers)).json()

    assert body["name"] == "pptx"
    assert body["description"] == "Build PowerPoint decks"


async def test_the_caller_cannot_rename_the_package(client, headers):
    """A name from outside would contradict the SKILL.md we just stored."""
    response = await upload(client, headers, name="my-pptx")

    # The form field no longer exists, so the extra value is simply ignored.
    assert response.json()["name"] == "pptx"


async def test_size_and_digest_describe_what_was_stored(client, headers, bucket):
    body = (await upload(client, headers)).json()
    stored = next(iter(bucket.values()))

    assert body["size_bytes"] == len(stored)
    assert body["sha256"] == service.sha256(stored)


async def test_the_storage_key_never_leaves_the_service(client, headers):
    body = (await upload(client, headers)).json()
    assert "storage_key" not in body

    listed = (await client.get("/v1/skills", headers=headers)).json()
    assert "storage_key" not in listed["data"][0]


async def test_an_uploaded_skill_shows_up_in_the_list(client, headers):
    await upload(client, headers)

    listed = (await client.get("/v1/skills", headers=headers)).json()
    assert [s["name"] for s in listed["data"]] == ["pptx"]


async def test_deleting_removes_the_object_too(client, headers, bucket):
    skill_id = (await upload(client, headers)).json()["id"]
    assert len(bucket) == 1

    response = await client.delete(f"/v1/skills/{skill_id}", headers=headers)

    assert response.status_code == 200
    assert bucket == {}


async def test_another_tenant_cannot_download_it(client, db, headers, other_tenant):
    other_id, other_headers = other_tenant
    skill_id = (await upload(client, headers)).json()["id"]

    response = await client.get(
        f"/v1/skills/{skill_id}/content",
        headers=other_headers,
    )
    assert response.status_code == 404


async def test_the_same_name_cannot_be_used_twice(client, headers):
    await upload(client, headers)

    response = await upload(client, headers)

    assert response.status_code == 409


# --- replacing what is in a skill --------------------------------------------


def read_member(package: bytes, path: str) -> bytes:
    """One file out of a downloaded package. The zip is deflated, so its
    contents are not findable in the raw bytes."""
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        return archive.read(path)


async def replace(client, headers, skill_id, content=None, **form):
    return await client.post(
        f"/v1/skills/{skill_id}",
        headers=headers,
        files=(
            {"file": ("pptx.zip", content, "application/zip")}
            if content is not None
            else None
        ),
        data=form,
    )


async def test_replacing_the_package_keeps_the_id(client, headers):
    """The reason this exists. An Agent names a skill by id, so changing what
    the skill contains must not mean re-pointing every Agent that uses it."""

    skill_id = (await upload(client, headers)).json()["id"]
    revised = make_zip({"SKILL.md": SKILL_MD, "scripts/run.py": b"print(2)\n"})

    response = await replace(client, headers, skill_id, revised)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == skill_id

    downloaded = await client.get(f"/v1/skills/{skill_id}/content", headers=headers)
    assert read_member(downloaded.content, "pptx/scripts/run.py") == b"print(2)\n"


async def test_a_replacement_restates_the_size_and_digest(client, headers, bucket):
    skill_id = (await upload(client, headers)).json()["id"]
    revised = make_zip({"SKILL.md": SKILL_MD, "notes.md": b"much longer content here\n"})

    body = (await replace(client, headers, skill_id, revised)).json()

    stored = bucket[
        next(k for k in bucket if body["sha256"] == service.sha256(bucket[k]))
    ]
    assert body["size_bytes"] == len(stored)


async def test_a_replacement_package_renames_the_skill(client, headers):
    """The package names it on the way in, the same as on create — a name
    stored beside a package that disagrees would not load in the sandbox."""

    skill_id = (await upload(client, headers)).json()["id"]
    renamed = make_zip(
        {"SKILL.md": SKILL_MD.replace(b"name: pptx", b"name: decks")}
    )

    body = (await replace(client, headers, skill_id, renamed)).json()

    assert body["id"] == skill_id
    assert body["name"] == "decks"


async def test_a_replacement_cannot_take_another_skills_name(client, headers):
    skill_id = (await upload(client, headers)).json()["id"]
    other = make_zip({"SKILL.md": SKILL_MD.replace(b"name: pptx", b"name: decks")})
    await client.post(
        "/v1/skills",
        headers=headers,
        files={"file": ("decks.zip", other, "application/zip")},
    )

    response = await replace(client, headers, skill_id, other)

    assert response.status_code == 409


async def test_a_replacement_that_is_not_a_package_leaves_the_old_one(client, headers):
    skill_id = (await upload(client, headers)).json()["id"]

    response = await replace(client, headers, skill_id, b"this is not a zip")

    assert response.status_code == 422
    downloaded = await client.get(f"/v1/skills/{skill_id}/content", headers=headers)
    assert read_member(downloaded.content, "pptx/scripts/run.py") == b"print(1)\n"


async def test_metadata_can_still_be_edited_without_a_package(client, headers):
    skill_id = (await upload(client, headers)).json()["id"]

    body = (await replace(client, headers, skill_id, description="Decks, faster")).json()

    assert body["description"] == "Decks, faster"
    assert body["name"] == "pptx"


# --- what gets rejected ------------------------------------------------------


async def test_something_that_is_not_a_zip_is_rejected(client, headers, bucket):
    response = await upload(client, headers, b"this is not a zip")

    assert response.status_code == 422
    assert bucket == {}, "a rejected package must not reach storage"


async def test_a_package_without_a_manifest_is_rejected(client, headers):
    response = await upload(client, headers, make_zip({"README.md": b"hi"}))

    assert response.status_code == 422
    assert "SKILL.md" in response.json()["detail"]


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "a/../../b.py"])
async def test_paths_that_climb_out_of_the_directory_are_rejected(client, headers, path):
    """These unpack into a sandbox, so a path is an instruction about where."""
    response = await upload(client, headers, make_zip({"SKILL.md": SKILL_MD, path: b"x"}))

    assert response.status_code == 422


async def test_a_zip_bomb_is_rejected(client, headers):
    """A few kilobytes that expand to gigabytes."""
    response = await upload(client, headers, make_zip({"SKILL.md": SKILL_MD, "big": b"\0" * 20_000_000}))

    assert response.status_code == 422


async def test_too_many_members_is_rejected(client, headers):
    files = {"SKILL.md": SKILL_MD} | {f"f{i}": b"x" for i in range(service.MAX_MEMBERS + 1)}

    response = await upload(client, headers, make_zip(files))

    assert response.status_code == 422


# --- how the agent gets them -------------------------------------------------


async def test_the_sandbox_fetches_a_skill_with_a_url_not_the_bytes(db, org, client, headers, monkeypatch):
    """No package content passes through this service, and the container never
    holds a credential that reaches anything but the one object."""
    from app.config import get_settings
    from app.utils import sandbox as sandbox_utils
    from app.utils.sandbox import Sandbox

    skill_id = (await upload(client, headers)).json()["id"]
    commands: list[str] = []

    async def _run(self, command, **kwargs):
        commands.append(command)
        return SimpleNamespace(exit_code=0, stdout="skills_installed=1 retries=0")

    async def _presigned(key, *, expires_in=300):
        return f"https://r2.example/{key}?expires={expires_in}"

    monkeypatch.setattr(Sandbox, "run", _run)
    monkeypatch.setattr("app.utils.storage.presigned_download_url", _presigned)

    await Sandbox.from_id("sbx_1", "ses_1", org).install_skills(db, [skill_id])

    assert len(commands) == 1, "one Skill set must use one E2B command"
    installer = commands[0]
    assert "https://r2.example/organizations/" in installer
    assert f"expires={get_settings().transfer_url_ttl_seconds}" in installer
    assert "download_one" in installer
    assert "wait_batch" in installer
    assert f"skills_dir={sandbox_utils.SKILLS_DIR}" in installer
    assert 'mv "$expanded" "$skills_dir"' in installer


async def test_a_reference_to_a_missing_skill_fails_loudly(db, org, monkeypatch):
    """Better than an agent quietly missing a capability it was configured with."""
    from app.utils.sandbox import Sandbox

    async def _run(self, command, **kwargs):
        return None

    monkeypatch.setattr(Sandbox, "run", _run)

    with pytest.raises(ValueError):
        await Sandbox.from_id("sbx_1", "ses_1", org).install_skills(db, ["skill_gone"])


async def test_a_failed_installer_command_fails_the_session_create_path(
    db, org, client, headers, monkeypatch
):
    """E2B raises on a nonzero shell exit; provider details stay internal."""
    from app.utils.sandbox import Sandbox

    skill_id = (await upload(client, headers)).json()["id"]

    async def _run(self, command, **kwargs):
        raise CommandExitException(
            stderr="download failed: secret=must-not-enter-errors",
            stdout="",
            exit_code=31,
            error=None,
        )

    async def _presigned(key, *, expires_in=300):
        return "https://r2.example/object?secret=must-not-enter-errors"

    monkeypatch.setattr(Sandbox, "run", _run)
    monkeypatch.setattr("app.utils.storage.presigned_download_url", _presigned)

    with pytest.raises(RuntimeError, match="complete Skill set") as raised:
        await Sandbox.from_id("sbx_1", "ses_1", org).install_skills(db, [skill_id])

    assert "secret" not in str(raised.value)


async def test_duplicate_skill_references_download_only_once(
    db, org, client, headers, monkeypatch
):
    from app.utils.sandbox import Sandbox

    skill_id = (await upload(client, headers)).json()["id"]
    signed: list[str] = []
    commands: list[str] = []

    async def _presigned(key, *, expires_in=300):
        signed.append(key)
        return "https://r2.example/object"

    async def _run(self, command, **kwargs):
        commands.append(command)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(Sandbox, "run", _run)
    monkeypatch.setattr("app.utils.storage.presigned_download_url", _presigned)

    await Sandbox.from_id("sbx_1", "ses_1", org).install_skills(
        db, [skill_id, skill_id]
    )

    assert len(signed) == 1
    assert commands[0].count("download_one 0 ") == 1


@pytest.mark.parametrize(
    "name",
    [
        "My Skill",       # spaces
        "MySkill",        # uppercase
        "my_skill",       # underscore
        "my--skill",      # consecutive hyphens
        "-myskill",       # leading hyphen
        "myskill-",       # trailing hyphen
        "..",             # would name the parent directory
        "a/b",            # would name a path
        "x" * 65,         # too long
    ],
)
async def test_a_name_that_would_not_load_is_rejected_at_upload(client, headers, name):
    """DeepAgents refuses to load a skill whose name breaks these rules.

    Rejecting here rather than sanitising is the point: a name we rewrote would
    no longer match its directory, so the skill would store fine and then
    silently never appear in the sandbox.
    """
    manifest = f"---\nname: {name}\ndescription: x\n---\n".encode()

    response = await upload(client, headers, make_zip({"SKILL.md": manifest}))

    assert response.status_code == 422, name


async def test_the_directory_must_match_the_declared_name(client, headers):
    response = await upload(client, headers, make_zip({"other/SKILL.md": SKILL_MD}))

    assert response.status_code == 422
    assert "other" in response.json()["detail"]


async def test_frontmatter_is_parsed_as_yaml(client, headers):
    """A hand-rolled `key: value` split cannot read this."""
    manifest = (
        b"---\n"
        b"name: yaml-skill\n"
        b"description: >-\n"
        b"  a description that runs\n"
        b"  across two lines\n"
        b"---\n"
    )

    body = (await upload(client, headers, make_zip({"SKILL.md": manifest}))).json()

    assert body["description"] == "a description that runs across two lines"


async def test_a_description_is_required(client, headers):
    manifest = b"---\nname: nodesc\n---\n"

    response = await upload(client, headers, make_zip({"SKILL.md": manifest}))

    assert response.status_code == 422


async def test_an_over_long_description_is_rejected(client, headers):
    manifest = f"---\nname: verbose\ndescription: {'x' * 1100}\n---\n".encode()

    response = await upload(client, headers, make_zip({"SKILL.md": manifest}))

    assert response.status_code == 422

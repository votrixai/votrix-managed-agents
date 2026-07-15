import io
import stat
import zipfile

import pytest

from app.config import get_settings
from tests.conftest import TEST_HEADERS


_FIXED_ZIP_DATE = (2024, 1, 1, 0, 0, 0)


def _backend_skill_zip(
    files: dict[str, bytes],
    *,
    folder: str = "skill",
) -> bytes:
    """Match votrix-backend's deterministic single-file ZIP upload shape."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path, content in sorted(files.items()):
            info = zipfile.ZipInfo(
                filename=f"{folder}/{relative_path}",
                date_time=_FIXED_ZIP_DATE,
            )
            archive.writestr(info, content)
    return buffer.getvalue()


def _compressed_skill_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


async def test_skill_upload_rejects_missing_description(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill/SKILL.md", b"---\nname: research\n---\nBody.", "text/markdown")},
    )

    assert response.status_code == 422
    assert "description" in response.json()["error"]["message"]


async def test_skill_upload_rejects_mixed_top_level_directories(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files=[
            (
                "files",
                (
                    "skill/SKILL.md",
                    b"---\nname: research\ndescription: Use sources.\n---\nBody.",
                    "text/markdown",
                ),
            ),
            ("files", ("other/schema.json", b"{}", "application/json")),
        ],
    )

    assert response.status_code == 422
    assert "top-level" in response.json()["error"]["message"]


async def test_skill_upload_rejects_unsafe_archive_paths(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("../skill/SKILL.md", b"---\nname: x\ndescription: x.\n---\nBody.", "text/markdown")},
    )

    assert response.status_code == 422
    assert "file paths" in response.json()["error"]["message"]

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("/skill/SKILL.md", b"---\nname: x\ndescription: x.\n---\nBody.", "text/markdown")},
    )

    assert response.status_code == 422
    assert "relative" in response.json()["error"]["message"]


async def test_skill_list_rejects_invalid_source_filter(client):
    response = await client.get("/v1/skills?source=third_party", headers=TEST_HEADERS)

    assert response.status_code == 422
    assert "source" in response.json()["error"]["message"]


async def test_skill_upload_persists_manifest_metadata(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: research\ndescription: Use sources.\n---\nBody.",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["name"] == "research"
    assert skill["description"] == "Use sources."
    assert skill["top_level_directory"] == "skill"
    assert skill["version"]["manifest"]["name"] == "research"


async def test_backend_single_zip_upload_creates_and_versions_skill(client):
    first_archive = _backend_skill_zip(
        {
            "SKILL.md": b"---\nname: research\ndescription: Backend ZIP skill.\n---\nFirst version.",
            "references/source.txt": b"source one",
        },
        folder="research-skill",
    )
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        data={"display_title": "Research Skill"},
        files={"files": ("skill.zip", first_archive, "application/zip")},
    )

    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["name"] == "research"
    assert skill["description"] == "Backend ZIP skill."
    assert skill["top_level_directory"] == "research-skill"
    assert [item["filename"] for item in skill["version"]["files"]] == [
        "research-skill/SKILL.md",
        "research-skill/references/source.txt",
    ]

    second_archive = _backend_skill_zip(
        {
            "SKILL.md": b"---\nname: research\ndescription: Backend ZIP skill v2.\n---\nSecond version.",
            "references/source.txt": b"source two",
        },
        folder="research-skill",
    )
    response = await client.post(
        f"/v1/skills/{skill['id']}/versions",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", second_archive, "application/zip")},
    )
    assert response.status_code == 201, response.text
    version = response.json()
    assert version["description"] == "Backend ZIP skill v2."

    download = await client.get(
        f"/v1/skills/{skill['id']}/versions/{version['version']}/content",
        headers=TEST_HEADERS,
    )
    assert download.status_code == 200, download.text
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == [
            "research-skill/SKILL.md",
            "research-skill/references/source.txt",
        ]
        assert b"Second version." in archive.read("research-skill/SKILL.md")


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../skill/SKILL.md",
        "skill/../SKILL.md",
        "/skill/SKILL.md",
        "C:/skill/SKILL.md",
        "skill\\..\\SKILL.md",
        "skill/control\nname.md",
        f"skill/{'a' * 256}",
    ],
)
async def test_backend_zip_upload_rejects_unsafe_member_paths(client, unsafe_path):
    archive = _compressed_skill_zip(
        {unsafe_path: b"---\nname: unsafe\ndescription: Unsafe path.\n---\nBody."}
    )

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", archive, "application/zip")},
    )

    assert response.status_code == 422, response.text
    assert "path" in response.json()["error"]["message"].lower()


async def test_backend_zip_upload_rejects_symlink_member(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "skill/SKILL.md",
            b"---\nname: unsafe\ndescription: Unsafe link.\n---\nBody.",
        )
        link = zipfile.ZipInfo("skill/reference-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", buffer.getvalue(), "application/zip")},
    )

    assert response.status_code == 422, response.text
    assert "symbolic link" in response.json()["error"]["message"]


async def test_backend_zip_upload_rejects_duplicate_and_mixed_top_level_paths(client):
    duplicate = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(duplicate, "w") as archive:
            content = b"---\nname: unsafe\ndescription: Duplicate path.\n---\nBody."
            archive.writestr("skill/SKILL.md", content)
            archive.writestr("skill/SKILL.md", content)
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", duplicate.getvalue(), "application/zip")},
    )
    assert response.status_code == 422, response.text
    assert "duplicate path" in response.json()["error"]["message"]

    mixed = _compressed_skill_zip(
        {
            "skill/SKILL.md": b"---\nname: unsafe\ndescription: Mixed roots.\n---\nBody.",
            "other/reference.txt": b"other",
        }
    )
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", mixed, "application/zip")},
    )
    assert response.status_code == 422, response.text
    assert "top-level" in response.json()["error"]["message"]


async def test_skill_zip_cannot_be_mixed_with_direct_multipart_files(client):
    archive = _backend_skill_zip(
        {"SKILL.md": b"---\nname: mixed\ndescription: Mixed upload.\n---\nBody."}
    )

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files=[
            ("files", ("skill.zip", archive, "application/zip")),
            ("files", ("skill/reference.txt", b"reference", "text/plain")),
        ],
    )

    assert response.status_code == 422, response.text
    assert "only multipart file" in response.json()["error"]["message"]


async def test_direct_multipart_skill_upload_rejects_duplicate_paths(client):
    content = b"---\nname: duplicate\ndescription: Duplicate direct upload.\n---\nBody."
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files=[
            ("files", ("skill/SKILL.md", content, "text/markdown")),
            ("files", ("skill/SKILL.md", content, "text/markdown")),
        ],
    )

    assert response.status_code == 422, response.text
    assert "duplicate paths" in response.json()["error"]["message"]


async def test_direct_multipart_skill_upload_enforces_aggregate_size(client, monkeypatch):
    monkeypatch.setenv("VMA_MAX_SKILL_ARCHIVE_BYTES", "100")
    get_settings.cache_clear()
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files=[
            (
                "files",
                (
                    "skill/SKILL.md",
                    b"---\nname: bounded\ndescription: Bounded direct upload.\n---\nBody.",
                    "text/markdown",
                ),
            ),
            ("files", ("skill/reference.txt", b"x" * 60, "text/plain")),
        ],
    )

    assert response.status_code == 413, response.text
    assert "maximum size" in response.json()["error"]["message"]


@pytest.mark.parametrize("archive", [b"not a zip", b"PK\x03\x04truncated"])
async def test_backend_zip_upload_rejects_malformed_archive(client, archive):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", archive, "application/zip")},
    )

    assert response.status_code == 422, response.text
    assert "valid ZIP archive" in response.json()["error"]["message"]


async def test_backend_zip_upload_rejects_crc_corruption(client):
    archive = bytearray(
        _compressed_skill_zip(
            {"skill/SKILL.md": b"---\nname: crc\ndescription: CRC check.\n---\nBody."}
        )
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as parsed:
        member = parsed.getinfo("skill/SKILL.md")
        offset = member.header_offset
        filename_length = int.from_bytes(archive[offset + 26 : offset + 28], "little")
        extra_length = int.from_bytes(archive[offset + 28 : offset + 30], "little")
        data_offset = offset + 30 + filename_length + extra_length
    archive[data_offset] ^= 0xFF

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", bytes(archive), "application/zip")},
    )

    assert response.status_code == 422, response.text
    assert "safely read" in response.json()["error"]["message"]


async def test_backend_zip_upload_rejects_expansion_beyond_limit(client, monkeypatch):
    monkeypatch.setenv("VMA_MAX_SKILL_ARCHIVE_BYTES", "512")
    get_settings.cache_clear()
    archive = _compressed_skill_zip(
        {
            "skill/SKILL.md": (
                b"---\nname: unsafe\ndescription: Expansion bomb.\n---\n" + b"A" * 4_096
            )
        }
    )
    assert len(archive) < 512

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", archive, "application/zip")},
    )

    assert response.status_code == 413, response.text
    assert "expanded content" in response.json()["error"]["message"]


async def test_backend_zip_upload_rejects_unsafe_compression_ratio(client, monkeypatch):
    import app.routers.skills as skills_router

    monkeypatch.setattr(skills_router, "MAX_SKILL_ZIP_COMPRESSION_RATIO", 2)
    archive = _compressed_skill_zip(
        {
            "skill/SKILL.md": (
                b"---\nname: unsafe\ndescription: Compression ratio.\n---\n" + b"A" * 1_024
            )
        }
    )

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={"files": ("skill.zip", archive, "application/zip")},
    )

    assert response.status_code == 422, response.text
    assert "compression ratio" in response.json()["error"]["message"]


async def test_agent_skill_references_are_validated_and_normalized(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: support\ndescription: Support skill.\n---\nUse it.",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    skill = response.json()

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Skill Agent",
            "model": {"id": "gpt-5.5"},
            "skills": [
                {"type": "skill", "id": skill["id"], "version": skill["latest_version"]},
                {"skill_id": skill["id"], "version": "latest"},
                {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    assert agent["skills"] == [
        {"type": "custom", "skill_id": skill["id"], "version": skill["latest_version"]},
        {"type": "custom", "skill_id": skill["id"], "version": "latest"},
        {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
    ]

    response = await client.patch(
        f"/v1/agents/{agent['id']}",
        headers=TEST_HEADERS,
        json={"version": agent["version"], "skills": [{"id": "skill_missing", "version": "latest"}]},
    )
    assert response.status_code == 422


async def test_session_snapshot_pins_inherited_latest_custom_skill_version(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: pinned\ndescription: Pinned skill.\n---\nVersion one.",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    skill = response.json()
    first_version = skill["latest_version"]

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={
            "name": "Latest Skill Agent",
            "model": {"id": "gpt-5.5"},
            "skills": [{"type": "custom", "skill_id": skill["id"], "version": "latest"}],
        },
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    assert agent["skills"][0]["version"] == "latest"

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "latest-skill-session", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={"agent": agent["id"], "environment_id": environment["id"]},
    )
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["agent"]["skills"][0]["version"] == first_version

    response = await client.post(
        f"/v1/skills/{skill['id']}/versions",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: pinned\ndescription: Pinned skill.\n---\nVersion two.",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["version"] != first_version

    response = await client.get(f"/v1/sessions/{session['id']}", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["agent"]["skills"][0]["version"] == first_version


async def test_skill_version_download_returns_zip_archive(client):
    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files=[
            (
                "files",
                (
                    "skill/SKILL.md",
                    b"---\nname: research\ndescription: Use sources.\n---\nBody.",
                    "text/markdown",
                ),
            ),
            ("files", ("skill/schema.json", b'{"type":"object"}', "application/json")),
        ],
    )

    assert response.status_code == 201, response.text
    skill = response.json()

    response = await client.get(
        f"/v1/skills/{skill['id']}/versions/{skill['version']['version']}/content",
        headers=TEST_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["skill/SKILL.md", "skill/schema.json"]
        assert b"Use sources." in archive.read("skill/SKILL.md")


async def test_skill_upload_size_limit(client, monkeypatch):
    monkeypatch.setenv("VMA_MAX_SKILL_ARCHIVE_BYTES", "100")
    get_settings.cache_clear()

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: research\ndescription: Use sources.\n---\nBody.",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 413
    assert "maximum size" in response.json()["error"]["message"]

import io
import zipfile

from app.config import get_settings
from tests.conftest import TEST_HEADERS


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

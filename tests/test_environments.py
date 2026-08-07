"""Environments: an image recipe, built once, started from many times.

The provider is stubbed — this is about when a build gets started, when it gets
asked about, and what a session is allowed to do while it is still going.
"""

from __future__ import annotations

import pytest

from app.db.queries import organizations
from app.db.queries import vma_api_keys as api_keys_q
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.deps import get_db
from app.services import environments as service
from app.utils import sandbox as sandbox_utils


@pytest_asyncio.fixture
async def client(db, builds):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def create(client, headers, **body):
    body.setdefault("name", "data-analysis")
    return await client.post("/v1/environments", headers=headers, json=body)


# --- with nothing to install -------------------------------------------------


async def test_an_environment_without_packages_needs_no_build(client, headers, builds):
    response = await create(client, headers)

    assert response.status_code == 201
    assert response.json()["build_state"] == "ready"
    assert builds.started == [], "nothing was declared, so nothing should be built"


async def test_a_session_can_use_it_immediately(client, headers, db, org, agent, sandboxes):
    """And it starts from the base image, which was written down at create time
    rather than worked out down in the sandbox layer."""
    environment = (await create(client, headers)).json()

    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={"agent_id": agent.id, "environment_id": environment["id"]},
    )
    assert response.status_code == 201, response.text
    assert [call["image"] for call in sandboxes] == [sandbox_utils.BASE_IMAGE]


# --- with packages -----------------------------------------------------------


async def test_declaring_packages_starts_a_build(client, headers, builds):
    response = await create(
        client, headers, config={"packages": {"pip": ["pandas"], "npm": ["express"]}}
    )

    assert response.json()["build_state"] == "building"
    assert builds.started[0]["packages"] == {"pip": ["pandas"], "npm": ["express"]}


async def test_the_image_is_named_after_the_environment_not_the_label(client, headers, builds):
    """Provider template names are global; two organizations may pick one label."""
    body = (await create(client, headers, name="my-favourite-name",
                         config={"packages": {"pip": ["pandas"]}})).json()

    assert builds.started[0]["name"] == body["id"]


async def test_reading_it_back_asks_whether_the_build_finished(client, headers, builds):
    """Nothing calls us when a build ends, so a read is what notices."""
    environment_id = (await create(client, headers,
                                   config={"packages": {"pip": ["pandas"]}})).json()["id"]

    builds.finish("ready")
    response = await client.get(f"/v1/environments/{environment_id}", headers=headers)

    assert response.json()["build_state"] == "ready"


async def test_a_failed_build_reports_why(client, headers, builds):
    environment_id = (await create(client, headers,
                                   config={"packages": {"pip": ["nope"]}})).json()["id"]

    builds.finish("failed", "No matching distribution found for nope")
    body = (await client.get(f"/v1/environments/{environment_id}", headers=headers)).json()

    assert body["build_state"] == "failed"
    assert "No matching distribution" in body["build_error"]


@pytest.mark.parametrize("state,error", [("building", None), ("failed", "boom")])
async def test_a_session_is_refused_until_the_image_is_ready(
    client, headers, builds, agent, state, error
):
    environment_id = (await create(client, headers,
                                   config={"packages": {"pip": ["pandas"]}})).json()["id"]
    builds.finish(state, error)

    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={"agent_id": agent.id, "environment_id": environment_id},
    )

    assert response.status_code == 409


# --- changing one ------------------------------------------------------------


async def test_renaming_does_not_rebuild(client, headers, builds):
    environment_id = (await create(client, headers,
                                   config={"packages": {"pip": ["pandas"]}})).json()["id"]
    assert len(builds.started) == 1

    await client.post(f"/v1/environments/{environment_id}", headers=headers,
                      json={"name": "renamed"})

    assert len(builds.started) == 1


async def test_the_machine_is_chosen_per_environment(client, headers, builds):
    """Baked into the image, so a session cannot ask for a different one."""
    await create(
        client, headers,
        config={"packages": {"pip": ["playwright"]}, "cpu": 4, "memory_mb": 4096},
    )

    assert builds.started[0]["cpu"] == 4
    assert builds.started[0]["memory_mb"] == 4096


async def test_a_bigger_machine_rebuilds(client, headers, builds):
    """cpu and memory are part of the image, not a runtime flag — asking for
    more of either has to produce a new one."""
    environment_id = (await create(client, headers,
                                   config={"packages": {"pip": ["pandas"]}})).json()["id"]

    await client.post(
        f"/v1/environments/{environment_id}",
        headers=headers,
        json={"config": {"packages": {"pip": ["pandas"]}, "cpu": 8}},
    )

    assert len(builds.started) == 2
    assert builds.started[1]["cpu"] == 8


async def test_changing_packages_rebuilds(client, headers, builds):
    environment_id = (await create(client, headers,
                                   config={"packages": {"pip": ["pandas"]}})).json()["id"]

    body = (await client.post(
        f"/v1/environments/{environment_id}",
        headers=headers,
        json={"config": {"packages": {"pip": ["pandas", "numpy"]}}},
    )).json()

    assert len(builds.started) == 2
    assert builds.started[1]["packages"] == {"pip": ["pandas", "numpy"]}
    assert body["build_state"] == "building"


# --- names, archiving, deleting ----------------------------------------------


async def test_names_are_unique_within_an_organization(client, headers):
    await create(client, headers, name="shared")

    response = await create(client, headers, name="shared")

    assert response.status_code == 409


async def test_two_organizations_may_use_the_same_name(client, headers, db, other_tenant):
    other_id, other_headers = other_tenant
    await create(client, headers, name="shared")

    response = await create(
        client,
        other_headers,
        name="shared",
    )

    assert response.status_code == 201


async def test_an_archived_environment_cannot_back_a_new_session(client, headers, agent):
    environment_id = (await create(client, headers)).json()["id"]
    await client.post(f"/v1/environments/{environment_id}/archive", headers=headers)

    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={"agent_id": agent.id, "environment_id": environment_id},
    )

    assert response.status_code == 409


async def test_an_environment_in_use_cannot_be_deleted(client, headers, db, agent, session):
    """`session` already points at an environment — archive it instead."""
    response = await client.delete(f"/v1/environments/{session.environment_id}", headers=headers)

    assert response.status_code == 409
    assert "session" in response.json()["error"]["message"]


async def test_an_unused_environment_can_be_deleted(client, headers):
    environment_id = (await create(client, headers, name="throwaway")).json()["id"]

    response = await client.delete(f"/v1/environments/{environment_id}", headers=headers)

    assert response.status_code == 200
    assert (await client.get(f"/v1/environments/{environment_id}", headers=headers)).status_code == 404


async def test_the_image_name_stays_internal(client, headers):
    body = (await create(client, headers, config={"packages": {"pip": ["pandas"]}})).json()

    for hidden in ("image_id", "build_id", "organization_id"):
        assert hidden not in body


# --- reading what the provider actually reports ------------------------------
#
# Everything above stubs the builder out, so nothing there exercises the one
# place we read the provider's own reply. That is where it went wrong.


@pytest.mark.parametrize(
    "reported,expected",
    [("ready", "ready"), ("error", "failed"), ("building", "building"), ("waiting", "building")],
)
async def test_the_build_status_enum_is_read_by_value(monkeypatch, reported, expected):
    """The provider reports status as an enum, and `str()` on it gives
    "TemplateBuildStatus.READY" — the member name, not the wire value. Read it
    without unwrapping and every build stays `building` forever, which leaves
    every environment that declared a package permanently unusable.
    """
    from e2b.template.types import TemplateBuildStatus

    class Reply:
        status = TemplateBuildStatus(reported)
        reason = "boom"

    async def fake_get_build_status(info, api_key=None):
        return Reply()

    monkeypatch.setattr(
        "app.utils.sandbox.AsyncTemplate.get_build_status", fake_get_build_status
    )

    status = await sandbox_utils.Image("img_1", "bld_1", "env_1").status()

    assert status.state == expected

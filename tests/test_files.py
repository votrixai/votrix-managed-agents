"""Files: what a session is given to work on.

Two things are being checked. That an upload is only usable once the bytes
really arrived — the client's word for it is not enough. And that an attached
file lands inside the sandbox's `uploads/` directory and cannot be talked into
landing anywhere else, because `skills/` is next door and the agent reads that
as instructions.

Object storage is a fake dict here. It is exercised for real at the end of
`scratchpad/` runs, not in the suite.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.deps import get_db
from app.utils import sandbox as sandbox_utils


class FakeBucket:
    """Stands in for R2. `uploaded` is what a client actually PUT."""

    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def put(self, key: str, data: bytes = b"hello") -> None:
        self.uploaded[key] = data

    async def save_bytes(self, data, *, organization_id, category, filename, mime_type=None):
        from app.utils import storage

        key = storage.object_key(
            organization_id=organization_id, category=category, filename=filename
        )
        self.uploaded[key] = data
        return storage.StoredObject(
            key=key,
            content_type=mime_type or "application/octet-stream",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    async def presigned_upload_url(self, key, *, mime_type, expires_in=900):
        return f"https://bucket.example/put/{key}"

    async def presigned_download_url(self, key, *, expires_in=300):
        return f"https://bucket.example/get/{key}"

    async def object_size(self, key):
        blob = self.uploaded.get(key)
        return None if blob is None else len(blob)

    async def delete_object(self, key):
        self.deleted.append(key)
        self.uploaded.pop(key, None)


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket()
    for name in ("save_bytes", "presigned_upload_url", "presigned_download_url",
                 "object_size", "delete_object"):
        monkeypatch.setattr(f"app.utils.storage.{name}", getattr(fake, name))
    return fake


@pytest_asyncio.fixture
def headers(org):
    return {"x-organization-id": org, "x-api-key": "anything"}


@pytest_asyncio.fixture
async def client(db, bucket, sandboxes):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def upload(client, headers, bucket=None, filename="sales.csv", content=b"hello"):
    """One call, as a client would do it."""
    response = await client.post(
        "/v1/files",
        headers=headers,
        files={"file": (filename, content, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- uploading ---------------------------------------------------------------


async def test_uploading_takes_one_call(client, headers):
    response = await client.post(
        "/v1/files",
        headers=headers,
        files={"file": ("sales.csv", b"region,revenue\n", "text/csv")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "sales.csv"
    assert body["mime_type"] == "text/csv"
    assert body["size_bytes"] == len(b"region,revenue\n")


async def test_what_is_recorded_is_measured_not_declared(client, headers):
    """The client says nothing about size or hash — both are read off the
    bytes, so there is nothing to take on trust and nothing to verify later."""
    content = b"twelve bytes"

    body = (await client.post(
        "/v1/files", headers=headers, files={"file": ("x.bin", content, "application/octet-stream")}
    )).json()

    assert body["size_bytes"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()


async def test_an_uploaded_file_has_no_scope(client, headers):
    """That label is what separates what a user put in from what a run made."""
    file_id = await upload(client, headers)

    body = (await client.get(f"/v1/files/{file_id}", headers=headers)).json()

    assert body["scope"] is None


# --- reading and removing ----------------------------------------------------


async def test_downloading_redirects_to_a_signed_url(client, headers, bucket):
    """The bytes never come through this service."""
    file_id = await upload(client, headers, bucket)

    response = await client.get(f"/v1/files/{file_id}/content", headers=headers)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://bucket.example/get/")


async def test_where_the_bytes_live_is_never_published(client, headers, bucket):
    """`storage_key` is ours. A caller reaches an object through a signed URL
    or not at all."""
    file_id = await upload(client, headers, bucket)

    body = (await client.get(f"/v1/files/{file_id}", headers=headers)).json()

    assert "storage_key" not in body
    assert "organization_id" not in body


async def test_another_tenant_cannot_see_the_file(client, headers, bucket, db):
    from app.db.queries import accounts

    file_id = await upload(client, headers, bucket)
    other = await accounts.create_organization(db, slug="other", name="Other")
    await db.commit()

    response = await client.get(
        f"/v1/files/{file_id}",
        headers={"x-organization-id": other.id, "x-api-key": "anything"},
    )

    assert response.status_code == 404


async def test_deleting_removes_the_bytes_as_well_as_the_row(client, headers, bucket):
    file_id = await upload(client, headers, bucket)

    await client.delete(f"/v1/files/{file_id}", headers=headers)

    assert bucket.deleted, "the object was left behind in the bucket"
    assert (await client.get(f"/v1/files/{file_id}", headers=headers)).status_code == 404


async def test_a_file_a_session_holds_cannot_be_deleted(
    client, headers, bucket, agent, environment, sandboxes
):
    """And the refusal has to come before anything is destroyed.

    `session_files` references the file, and sessions are only soft-deleted, so
    that reference outlives the session for good — the delete was never going
    to succeed. It used to be attempted anyway: the bytes went first, the row
    delete then hit the foreign key, and the rollback left a file that appeared
    in every listing and 404ed on every download. The bucket assertion below is
    the half that matters.
    """
    file_id = await upload(client, headers, bucket)
    await attach(client, headers, agent, environment, [{"file_id": file_id}])

    response = await client.delete(f"/v1/files/{file_id}", headers=headers)

    assert response.status_code == 409, response.text
    assert not bucket.deleted, "the bytes were destroyed by a delete that failed"
    assert (await client.get(f"/v1/files/{file_id}", headers=headers)).status_code == 200


# --- attaching one to a session ----------------------------------------------


async def attach(client, headers, agent, environment, resources):
    return await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "resources": resources,
        },
    )


async def test_an_attached_file_is_fetched_into_uploads(
    client, headers, bucket, agent, environment, sandboxes
):
    file_id = await upload(client, headers, bucket)

    response = await attach(client, headers, agent, environment,
                            [{"type": "file", "file_id": file_id}])

    assert response.status_code == 201, response.text
    # The sandbox is told which file and where, and resolves the rest itself —
    # nothing above that layer signs a URL or sees a bucket path.
    assert sandboxes[0]["files"] == [(file_id, "sales.csv")]


async def test_a_path_may_name_a_subdirectory(
    client, headers, bucket, agent, environment, sandboxes
):
    file_id = await upload(client, headers, bucket)

    await attach(client, headers, agent, environment,
                 [{"type": "file", "file_id": file_id, "path": "raw/q3.csv"}])

    assert [path for _, path in sandboxes[0]["files"]] == ["raw/q3.csv"]


@pytest.mark.parametrize("path", ["../skills/pptx/SKILL.md", "/etc/passwd", "a/../../b", ".."])
async def test_a_file_cannot_be_placed_outside_uploads(
    client, headers, bucket, agent, environment, path
):
    """`skills/` sits beside `uploads/` and the agent reads it as instructions,
    so an upload that could reach it would be an upload that rewrites the agent.
    """
    file_id = await upload(client, headers, bucket)

    response = await attach(client, headers, agent, environment,
                            [{"type": "file", "file_id": file_id, "path": path}])

    assert response.status_code == 409, f"{path!r} was accepted"


async def test_two_files_cannot_claim_the_same_path(
    client, headers, bucket, agent, environment
):
    first = await upload(client, headers, bucket, "a.csv")
    second = await upload(client, headers, bucket, "b.csv")

    response = await attach(
        client, headers, agent, environment,
        [
            {"type": "file", "file_id": first, "path": "data.csv"},
            {"type": "file", "file_id": second, "path": "data.csv"},
        ],
    )

    assert response.status_code == 409


async def test_another_tenants_file_cannot_be_attached(
    client, headers, bucket, agent, environment, db
):
    response = await attach(client, headers, agent, environment,
                            [{"type": "file", "file_id": "file_someone_else"}])

    assert response.status_code == 404


async def test_a_resource_we_do_not_support_is_refused_not_ignored(
    client, headers, agent, environment
):
    """A dropped resource is a session that quietly starts without what it was
    promised."""
    response = await attach(client, headers, agent, environment,
                            [{"type": "github_repository", "file_id": "x"}])

    assert response.status_code == 422


async def test_what_was_attached_is_recorded(
    client, headers, bucket, agent, environment, db
):
    """The turn reads this back to tell the agent what it has."""
    from app.db.queries import sessions as sessions_q

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment,
                               [{"type": "file", "file_id": file_id}])).json()["id"]

    rows = await sessions_q.list_session_files(db, session_id=session_id)

    assert [(row.file_id, row.path) for row in rows] == [(file_id, "sales.csv")]


async def test_a_session_with_no_resources_still_works(
    client, headers, agent, environment, sandboxes
):
    response = await attach(client, headers, agent, environment, [])

    assert response.status_code == 201
    assert sandboxes[0]["files"] == []


# --- what the agent is told --------------------------------------------------


def test_the_agent_is_told_where_its_files_are():
    """`uploads/` is read-only and easy to miss, and nothing about the
    directory says the user put something there on purpose."""
    from app.runtime.engine import _system_prompt

    prompt = _system_prompt("You are a helpful bot.", ["sales.csv"])

    assert "You are a helpful bot." in prompt
    assert f"{sandbox_utils.UPLOADS_DIR}/sales.csv" in prompt


def test_an_agent_with_nothing_attached_is_told_so():
    from app.runtime.engine import _system_prompt

    prompt = _system_prompt(None, [])

    assert sandbox_utils.UPLOADS_DIR in prompt
    assert "no files" in prompt


# --- collecting what the agent produced --------------------------------------
#
# The container is only reliably awake at the end of a turn, so that is when
# `outputs/` is taken. These fake the container's side of that.


class FakeContainer:
    """A `Sandbox` whose filesystem is a dict.

    `write` is the agent leaving something in `outputs/`; the recorded `curl`
    commands are the container pushing it to storage.
    """

    def __init__(self, bucket, session_id, organization_id) -> None:
        from app.utils.sandbox import Sandbox

        self.files: dict[str, bytes] = {}
        self.bucket = bucket
        self.pushed: list[str] = []
        self._sandbox = Sandbox.from_id("sbx_fake", session_id, organization_id)
        self._sandbox.run = self.run  # type: ignore[method-assign]

    def write(self, path: str, content: bytes) -> None:
        self.files[path] = content

    async def run(self, command, **kwargs):
        import hashlib
        import re
        import shlex as _shlex
        from types import SimpleNamespace

        from app.utils.sandbox import OUTPUTS_DIR

        if command.startswith("cd ") and "sha256sum" in command:
            # `<size> <sha256>  ./<path>` — one line per file, size and hash
            # from the same command. The real one gets both in a single round
            # trip because each extra one costs most of a second.
            lines = [
                f"{len(blob)} {hashlib.sha256(blob).hexdigest()}  ./{path}"
                for path, blob in sorted(self.files.items())
            ]
            return SimpleNamespace(stdout="\n".join(lines), stderr="", exit_code=0)
        if command.startswith("sha256sum "):
            path = _shlex.split(command)[1].removeprefix(f"{OUTPUTS_DIR}/")
            blob = self.files.get(path)
            out = "" if blob is None else f"{hashlib.sha256(blob).hexdigest()}  {path}"
            return SimpleNamespace(stdout=out, exit_code=0)
        if "curl" in command and "-X PUT" in command:
            source = re.search(r"-T (\S+)", command).group(1).strip("'\"")
            url = command.rsplit(" ", 1)[-1].strip("'\"")
            key = url.removeprefix("https://bucket.example/put/")
            self.pushed.append(key)
            self.bucket.put(key, self.files[source.removeprefix(f"{OUTPUTS_DIR}/")])
            return SimpleNamespace(stdout="", exit_code=0)
        return SimpleNamespace(stdout="", exit_code=0)

    def __getattr__(self, name):
        return getattr(self._sandbox, name)


@pytest.fixture
def container(bucket, org):
    return FakeContainer(bucket, "ses_1", org)


async def collect(db, container):
    return await container._sandbox.discover_outputs(db)


async def test_what_the_agent_left_becomes_a_file(db, org, bucket, container):
    container.write("chart.png", b"PNG-bytes")

    collected = await collect(db, container)

    assert [f.filename for f in collected] == ["chart.png"]
    assert collected[0].size_bytes == len(b"PNG-bytes")
    assert collected[0].sha256 == hashlib.sha256(b"PNG-bytes").hexdigest()


async def test_a_collected_file_says_which_session_made_it(db, org, bucket, container):
    """That label is the whole difference between an output and an upload."""
    container.write("chart.png", b"PNG-bytes")

    collected = await collect(db, container)

    assert collected[0].scope_id == "ses_1"


async def test_the_same_file_is_not_taken_twice(db, org, bucket, container):
    """Collection runs after every turn, and a session has many."""
    container.write("chart.png", b"PNG-bytes")
    await collect(db, container)

    collected = await collect(db, container)

    assert collected == []


async def test_a_rewritten_file_is_taken_again(db, org, bucket, container):
    container.write("chart.png", b"first draft")
    first = (await collect(db, container))[0]

    container.write("chart.png", b"second draft, much better")
    second = (await collect(db, container))[0]

    assert second.id != first.id
    assert second.size_bytes == len(b"second draft, much better")


async def test_an_id_handed_out_keeps_working_after_a_rewrite(db, org, bucket, container):
    """Captures are added, never replaced.

    An id quoted to a user, or stored by a client, has to keep resolving. If a
    later capture removed the record it superseded, every id handed out earlier
    in the session would rot the moment the agent touched the file again.
    """
    from app.services import files as files_service

    container.write("chart.png", b"first draft")
    first = (await collect(db, container))[0]

    container.write("chart.png", b"second draft")
    await collect(db, container)

    still_there = await files_service.get_file(db, file_id=first.id, organization_id=org)
    assert still_there.size_bytes == len(b"first draft")
    assert not bucket.deleted, "an earlier version was removed from the bucket"


async def test_a_list_shows_the_newest_version_first(db, org, bucket, container, client, headers):
    container.write("chart.png", b"first draft")
    await collect(db, container)
    container.write("chart.png", b"second draft")
    await collect(db, container)

    body = (await client.get("/v1/files?scope_id=ses_1", headers=headers)).json()

    assert [f["size_bytes"] for f in body["data"]] == [
        len(b"second draft"), len(b"first draft")
    ]


async def test_files_in_subdirectories_keep_their_place(db, org, bucket, container):
    container.write("charts/q3.png", b"PNG-bytes")

    collected = await collect(db, container)

    assert [f.filename for f in collected] == ["charts/q3.png"]


async def test_a_huge_file_is_left_behind(db, org, bucket, container, monkeypatch):
    """An agent can fill a disk; collection is not obliged to follow it."""
    from app.config import clear_settings_cache

    monkeypatch.setenv("MAX_OUTPUT_BYTES", "8")
    clear_settings_cache()
    container.write("huge.bin", b"far more than eight bytes")

    assert await collect(db, container) == []
    clear_settings_cache()


async def test_collection_failure_does_not_fail_the_turn(db, org, session, monkeypatch):
    """The agent did the work and the events are written. What is lost is the
    delivery, so it is reported as an event rather than as a failed turn."""
    from app.db.queries import sessions as sessions_q
    from app.services import sessions as service
    from app.utils.sandbox import Sandbox

    async def _boom(self, db):
        raise RuntimeError("the container went away")

    monkeypatch.setattr(Sandbox, "discover_outputs", _boom)

    await service.collect_outputs(
        db, session, Sandbox.from_id("sbx_1", session.id, org)
    )

    events = (await sessions_q.list_events(db, session_id=session.id, organization_id=org)).items
    assert events[-1].payload["error"]["type"] == "output_collection_failed"


async def test_outputs_are_listed_by_session(client, headers, db, org, bucket, container):
    """`?scope_id=` is how a caller asks for one run's results rather than
    everything the account has."""
    container.write("chart.png", b"PNG-bytes")
    await collect(db, container)

    scoped = (await client.get("/v1/files?scope_id=ses_1", headers=headers)).json()["data"]
    everything = (await client.get("/v1/files", headers=headers)).json()["data"]

    assert [f["filename"] for f in scoped] == ["chart.png"]
    assert [f["filename"] for f in everything] == ["chart.png"]


async def test_another_sessions_outputs_are_not_included(client, headers, db, org, bucket, container):
    container.write("chart.png", b"PNG-bytes")
    await collect(db, container)

    body = (await client.get("/v1/files?scope_id=ses_other", headers=headers)).json()

    assert body["data"] == []


# --- taking a file before the turn is over -----------------------------------
#
# Outputs are collected when a turn ends, which is no use to a client watching
# a long one. `live/files` is the other case: the agent has finished something
# and somebody wants it now.


@pytest_asyncio.fixture
async def running(client, headers, agent, environment, db, container, monkeypatch):
    """A session whose sandbox is the fake container."""
    from app.utils.sandbox import Sandbox

    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    container._sandbox = Sandbox.from_id("sbx_fake", session_id, container._sandbox.organization_id)
    container._sandbox.run = container.run
    monkeypatch.setattr(Sandbox, "from_id", classmethod(lambda cls, *a: container._sandbox))
    return session_id


async def take(client, headers, session_id, path):
    return await client.post(
        f"/v1/sessions/{session_id}/live/files", headers=headers, json={"path": path}
    )


async def test_a_file_can_be_taken_mid_turn(client, headers, running, container):
    container.write("report.pdf", b"%PDF-fake")

    response = await take(client, headers, running, "report.pdf")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "report.pdf"
    assert body["size_bytes"] == len(b"%PDF-fake")
    assert body["scope"] == {"type": "session", "id": running}


async def test_a_taken_file_downloads_like_any_other(client, headers, running, container):
    """It is not a special kind of thing — the same record, the same route."""
    container.write("report.pdf", b"%PDF-fake")
    file_id = (await take(client, headers, running, "report.pdf")).json()["id"]

    response = await client.get(f"/v1/files/{file_id}/content", headers=headers)

    assert response.status_code == 307


async def test_taking_the_same_file_twice_makes_one_record(client, headers, running, container):
    """A client that retried, or a tool called twice, must not multiply rows."""
    container.write("report.pdf", b"%PDF-fake")

    first = (await take(client, headers, running, "report.pdf")).json()
    second = (await take(client, headers, running, "report.pdf")).json()

    assert first["id"] == second["id"]


async def test_taking_it_again_after_a_change_makes_a_new_record(client, headers, running, container):
    container.write("report.pdf", b"draft")
    first = (await take(client, headers, running, "report.pdf")).json()

    container.write("report.pdf", b"final version")
    second = (await take(client, headers, running, "report.pdf")).json()

    assert second["id"] != first["id"]
    assert second["size_bytes"] == len(b"final version")


async def test_the_turn_end_collection_does_not_take_it_again(db, client, headers, running, container):
    """Both paths compare the same hash, so whichever runs second finds
    nothing to do rather than making a duplicate."""
    container.write("report.pdf", b"%PDF-fake")
    await take(client, headers, running, "report.pdf")

    assert await collect(db, container) == []


async def test_a_file_that_is_not_there_is_a_404(client, headers, running):
    response = await take(client, headers, running, "nothing.pdf")

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["../skills/pptx/SKILL.md", "/etc/passwd", "a/../../b"])
async def test_only_outputs_can_be_taken(client, headers, running, container, path):
    """The rest of the container is the user's own input and the instructions
    the agent runs on. Neither is this endpoint's business."""
    response = await take(client, headers, running, path)

    assert response.status_code == 409


async def test_taking_from_a_session_with_no_sandbox_is_refused(client, headers, agent, environment, db):
    """The file was in a container that no longer exists. Anything already
    collected is on `/v1/files` instead."""
    from app.db.queries import sessions as sessions_q

    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    sandbox = await sessions_q.get_sandbox(
        db, session_id=session_id, organization_id=(await client.get(
            f"/v1/sessions/{session_id}", headers=headers)).json() and headers["x-organization-id"]
    )
    await sessions_q.update_sandbox_state(db, sandbox, state="terminated")
    await db.commit()

    response = await take(client, headers, session_id, "report.pdf")

    assert response.status_code == 409


async def test_the_sandbox_fetches_an_attached_file_itself(
    db, org, bucket, client, headers, monkeypatch
):
    """The container pulls its own input with a signed single-object URL — no
    file content passes through this service and it holds no credential."""
    from app.utils.sandbox import UPLOADS_DIR, Sandbox

    file_id = await upload(client, headers, bucket)
    commands: list[str] = []

    async def _run(self, command, **kwargs):
        commands.append(command)
        return None

    monkeypatch.setattr(Sandbox, "run", _run)
    await Sandbox.from_id("sbx_1", "ses_1", org).upload_file(
        db, f"{UPLOADS_DIR}/sales.csv", file_id
    )

    curl = next(c for c in commands if "curl" in c)
    assert "https://bucket.example/get/" in curl
    # Read-only once it lands: an agent that edited an input would leave the
    # client's own copy disagreeing with what the run actually used.
    assert "chmod 444" in curl


async def test_fetching_a_file_that_does_not_exist_fails_loudly(db, org, monkeypatch):
    """Better than a session that starts believing it has an input it has not."""
    from app.utils.sandbox import UPLOADS_DIR, Sandbox

    async def _run(self, command, **kwargs):
        return None

    monkeypatch.setattr(Sandbox, "run", _run)
    with pytest.raises(ValueError):
        await Sandbox.from_id("sbx_1", "ses_1", org).upload_file(
            db, f"{UPLOADS_DIR}/x", "file_gone"
        )


def test_backend_forwards_every_async_protocol_method():
    """The list of forwarded methods has to stay complete by itself.

    Anything DeepAgents calls that this backend does not forward reaches the
    protocol's own version, which raises. That is not a loud failure in a
    useful place — it surfaced as skills that installed correctly into the
    sandbox and then silently never reached the model, because
    `SkillsMiddleware` reads them through `adownload_files` and nothing else
    ever called it.

    So this asserts the shape rather than any one method: every async method
    the protocol defines is overridden here.
    """
    import inspect

    from deepagents.backends.protocol import SandboxBackendProtocol

    from app.utils.sandbox import LazyE2BBackend

    expected = {
        name
        for name, member in inspect.getmembers(SandboxBackendProtocol, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    missing = sorted(expected - set(LazyE2BBackend.__dict__))

    assert not missing, (
        f"LazyE2BBackend does not forward {missing} — DeepAgents calling any of "
        f"them gets the protocol's NotImplementedError instead of the sandbox"
    )

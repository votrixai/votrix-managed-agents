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

from app.db.queries import organizations

from app.db.queries import vma_api_keys as api_keys_q
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
        # What each download URL was signed to say, which is the only place
        # the response's filename and disposition are decided.
        self.signed: list[dict] = []

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

    async def presigned_download_url(self, key, *, expires_in=300, filename=None, inline=False):
        self.signed.append({"key": key, "filename": filename, "inline": inline})
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


async def test_retrying_a_file_upload_with_the_same_key_returns_the_original(
    client, headers, bucket
):
    first = await client.post(
        "/v1/files",
        headers=headers,
        files={
            "file": ("legacy.md", b"first", "text/markdown"),
            "idempotency_key": (None, "cma-snapshot/file-123"),
        },
    )
    second = await client.post(
        "/v1/files",
        headers=headers,
        files={
            "file": ("ignored.md", b"different", "text/markdown"),
            "idempotency_key": (None, "cma-snapshot/file-123"),
        },
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["sha256"] == hashlib.sha256(b"first").hexdigest()
    assert len(bucket.uploaded) == 1


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


async def test_a_download_url_carries_the_file_s_own_name(client, headers, bucket):
    """A key starts with a content fingerprint, so without this the browser
    saves `a1b2c3d4_sales.csv`. The name only exists on the row."""
    await upload(client, headers, bucket, filename="sales.csv")
    file_id = await upload(client, headers, bucket, filename="sales.csv")

    await client.get(f"/v1/files/{file_id}/content", headers=headers)

    assert bucket.signed[-1]["filename"] == "sales.csv"
    assert bucket.signed[-1]["inline"] is False


async def test_a_caller_choosing_inline_gets_it_signed_inline(client, headers, bucket):
    """Only the caller knows whether this is a download button or an <img>."""
    file_id = await upload(client, headers, bucket, filename="chart.png")

    response = await client.get(
        f"/v1/files/{file_id}/content", params={"disposition": "inline"}, headers=headers
    )

    assert response.status_code == 307
    assert bucket.signed[-1]["inline"] is True


async def test_a_name_a_header_cannot_spell_survives_being_one():
    """Headers are latin-1 and an agent names files after the work. Both forms
    go out, per RFC 6266, so neither browser gets nothing."""
    from app.utils.storage import _content_disposition

    header = _content_disposition("attachment", "\u9500\u552e\u62a5\u8868.csv")

    header.encode("latin-1")  # would raise before the fallback existed
    assert 'filename="____.csv"' in header
    assert "filename*=UTF-8''%E9%94%80%E5%94%AE%E6%8A%A5%E8%A1%A8.csv" in header


async def test_where_the_bytes_live_is_never_published(client, headers, bucket):
    """`storage_key` is ours. A caller reaches an object through a signed URL
    or not at all."""
    file_id = await upload(client, headers, bucket)

    body = (await client.get(f"/v1/files/{file_id}", headers=headers)).json()

    assert "storage_key" not in body
    assert "organization_id" not in body


async def test_another_tenant_cannot_see_the_file(client, headers, bucket, db, other_tenant):
    other_id, other_headers = other_tenant
    from app.db.queries import organizations

    file_id = await upload(client, headers, bucket)

    response = await client.get(
        f"/v1/files/{file_id}",
        headers=other_headers,
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


# --- attaching one after the session exists --------------------------------


@pytest.fixture
def live_uploader(monkeypatch):
    """The E2B side of a durable File -> live sandbox transfer."""
    from types import SimpleNamespace

    from app.utils.sandbox import Sandbox

    state = SimpleNamespace(calls=[], prepared=0, error=None)

    class LiveSandbox:
        async def prepare_directories(self):
            state.prepared += 1

        async def upload_file(self, db, path, file_id):
            state.calls.append((path, file_id))
            if state.error is not None:
                raise state.error

    monkeypatch.setattr(
        Sandbox,
        "from_id",
        classmethod(lambda cls, *args: LiveSandbox()),
    )
    return state


async def attach_live(client, headers, session_id, file_id, path=None):
    body = {"file_id": file_id}
    if path is not None:
        body["path"] = path
    return await client.post(
        f"/v1/sessions/{session_id}/live/uploads",
        headers=headers,
        json=body,
    )


async def test_a_durable_file_can_be_added_to_an_existing_session(
    client,
    headers,
    bucket,
    agent,
    environment,
    live_uploader,
):
    from app.utils.sandbox import UPLOADS_DIR

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]

    response = await attach_live(client, headers, session_id, file_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_id"] == file_id
    assert body["mount_path"] == f"{UPLOADS_DIR}/sales.csv"
    assert live_uploader.prepared == 1
    assert live_uploader.calls == [(f"{UPLOADS_DIR}/sales.csv", file_id)]
    session = (await client.get(f"/v1/sessions/{session_id}", headers=headers)).json()
    assert len(session["resources"]) == 1
    assert session["resources"][0]["id"] == body["id"]
    assert session["resources"][0]["file_id"] == file_id
    assert session["resources"][0]["mount_path"] == body["mount_path"]


async def test_a_live_upload_may_name_a_subdirectory(
    client, headers, bucket, agent, environment, live_uploader
):
    from app.utils.sandbox import UPLOADS_DIR

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]

    response = await attach_live(
        client, headers, session_id, file_id, path="references/q3/sales.csv"
    )

    assert response.status_code == 201, response.text
    assert response.json()["mount_path"] == f"{UPLOADS_DIR}/references/q3/sales.csv"


async def test_retrying_the_same_live_upload_is_idempotent(
    client, headers, bucket, agent, environment, live_uploader, db
):
    from app.db.queries import sessions as sessions_q

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]

    first = await attach_live(client, headers, session_id, file_id)
    second = await attach_live(client, headers, session_id, file_id)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert live_uploader.prepared == 1
    assert len(live_uploader.calls) == 1
    rows = await sessions_q.list_session_files(db, session_id=session_id)
    assert [(row.file_id, row.path) for row in rows] == [(file_id, "sales.csv")]


async def test_a_live_upload_does_not_overwrite_an_occupied_path(
    client, headers, bucket, agent, environment, live_uploader
):
    first = await upload(client, headers, bucket, "first.csv")
    second = await upload(client, headers, bucket, "second.csv")
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    assert (
        await attach_live(client, headers, session_id, first, path="data.csv")
    ).status_code == 201

    response = await attach_live(client, headers, session_id, second, path="data.csv")

    assert response.status_code == 409
    assert "already attached at" in response.json()["error"]["message"]
    assert len(live_uploader.calls) == 1


async def test_two_files_of_one_name_land_beside_each_other(
    client, headers, bucket, agent, environment, live_uploader, db
):
    """No path given means no opinion about the path, so a clash is not an error.

    Naming is decided here rather than by the caller retrying: this is the side
    that holds the uniqueness constraint and has already read the bindings, so
    it can pick a free name in the work it was doing anyway. A caller stepping
    the name itself pays a refused round trip to learn each name it cannot have.

    An explicit `path` is a different request and still refuses — see above.
    """
    from app.db.queries import sessions as sessions_q
    from app.utils.sandbox import UPLOADS_DIR

    first = await upload(client, headers, bucket, "report.csv")
    second = await upload(client, headers, bucket, "report.csv")
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]

    one = await attach_live(client, headers, session_id, first)
    two = await attach_live(client, headers, session_id, second)

    assert (one.status_code, two.status_code) == (201, 201)
    assert one.json()["mount_path"] == f"{UPLOADS_DIR}/report.csv"
    assert two.json()["mount_path"] == f"{UPLOADS_DIR}/report_1.csv"

    rows = await sessions_q.list_session_files(db, session_id=session_id)
    assert sorted(row.path for row in rows) == ["report.csv", "report_1.csv"]
    # Both sets of bytes are in the container, at the names just reported.
    assert [call[0] for call in live_uploader.calls] == [
        f"{UPLOADS_DIR}/report.csv",
        f"{UPLOADS_DIR}/report_1.csv",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "../skills/pptx/SKILL.md",
        "/etc/passwd",
        "a/../../b",
        "a//b",
        "a/./b",
        "windows\\path.txt",
        "nul\x00byte.txt",
    ],
)
async def test_a_live_upload_cannot_escape_or_ambiguously_name_uploads(
    client, headers, bucket, agent, environment, live_uploader, path
):
    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]

    response = await attach_live(client, headers, session_id, file_id, path=path)

    assert response.status_code == 409, f"{path!r} was accepted"
    assert live_uploader.calls == []


async def test_a_busy_session_still_accepts_a_live_upload(
    client, headers, bucket, agent, environment, live_uploader, db
):
    """Attaching does not wait for the agent to stop talking.

    This used to be a 409. It was refused so that no turn could observe a file
    that had bytes in the container but no binding yet — a state the transaction
    already rules out on its own, since the binding is committed only after the
    copy succeeds. What the refusal actually cost was every attachment queueing
    behind every other one, on the path between pressing send and the agent
    starting.

    A turn already running has snapshotted `session_files` for its prompt and
    will not see this file. The next one will.
    """
    from app.db.queries import sessions as sessions_q

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    assert await sessions_q.claim_session(db, session_id=session_id)
    await db.commit()

    response = await attach_live(client, headers, session_id, file_id)

    assert response.status_code == 201
    assert len(live_uploader.calls) == 1


async def test_an_archived_session_refuses_a_live_upload(
    client, headers, bucket, agent, environment, live_uploader
):
    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    await client.post(f"/v1/sessions/{session_id}/archive", headers=headers)

    response = await attach_live(client, headers, session_id, file_id)

    assert response.status_code == 409
    assert live_uploader.calls == []


async def test_a_live_upload_requires_a_usable_sandbox(
    client, headers, bucket, agent, environment, live_uploader, db, org
):
    from app.db.queries import sessions as sessions_q

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    sandbox = await sessions_q.get_sandbox(
        db, session_id=session_id, organization_id=org
    )
    await sessions_q.update_sandbox_state(db, sandbox, state="terminated")
    await db.commit()

    response = await attach_live(client, headers, session_id, file_id)

    assert response.status_code == 409
    assert live_uploader.calls == []


async def test_another_tenants_file_cannot_be_live_uploaded(
    client, headers, agent, environment, live_uploader
):
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]

    response = await attach_live(client, headers, session_id, "file_someone_else")

    assert response.status_code == 404
    assert live_uploader.calls == []


async def test_a_failed_live_transfer_leaves_no_resource_binding(
    client, headers, bucket, agent, environment, live_uploader, db
):
    from app.db.queries import sessions as sessions_q

    file_id = await upload(client, headers, bucket)
    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    live_uploader.error = RuntimeError("E2B is unavailable")

    response = await attach_live(client, headers, session_id, file_id)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "sandbox_unavailable"
    assert await sessions_q.list_session_files(db, session_id=session_id) == []


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

    def remove(self, path: str) -> None:
        """The agent deleting a deliverable it had produced."""
        self.files.pop(path, None)

    def clear(self) -> None:
        """A container that answers a listing with nothing.

        Indistinguishable, from the outside, from one whose `outputs/` really
        is empty — which is why nothing is retired on the strength of it.
        """
        self.files.clear()

    async def run(self, command, **kwargs):
        import hashlib
        import re
        import shlex as _shlex
        from types import SimpleNamespace

        from app.utils.sandbox import OUTPUTS_DIR, _DIGEST_MISSING

        if command.startswith("cd ") and "sha256sum" in command:
            # `<size> <sha256>  ./<path>` — one line per file, size and hash
            # from the same command. The real one gets both in a single round
            # trip because each extra one costs most of a second.
            lines = [
                f"{len(blob)} {hashlib.sha256(blob).hexdigest()}  ./{path}"
                for path, blob in sorted(self.files.items())
            ]
            return SimpleNamespace(stdout="\n".join(lines), stderr="", exit_code=0)
        if command.startswith("if [ ! -e "):
            # `_digest` asks in one shell line whether the file is there and,
            # if it is, what it hashes to — because an empty answer used to
            # have to mean both "no such file" and "the hasher fell over".
            path = _shlex.split(command)[4].removeprefix(f"{OUTPUTS_DIR}/")
            blob = self.files.get(path)
            if blob is None:
                return SimpleNamespace(stdout=f"{_DIGEST_MISSING}\n", exit_code=0)
            return SimpleNamespace(
                stdout=f"{hashlib.sha256(blob).hexdigest()}  {path}", exit_code=0
            )
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


async def test_a_rewritten_file_keeps_its_identity(db, org, bucket, container):
    """A path is one file however many times its contents are rewritten."""
    container.write("chart.png", b"first draft")
    first = (await collect(db, container))[0]

    container.write("chart.png", b"second draft, much better")
    second = (await collect(db, container))[0]

    assert second.id == first.id
    assert second.size_bytes == len(b"second draft, much better")


async def test_an_id_handed_out_keeps_working_after_a_rewrite(db, org, bucket, container):
    """An id quoted to a user, or stored by a client, has to keep resolving —
    and to resolve to what is at that path now. Those are the two halves of
    what a file path means, and the id is how this service spells one."""
    from app.services import files as files_service

    container.write("chart.png", b"first draft")
    first = (await collect(db, container))[0]

    container.write("chart.png", b"second draft")
    await collect(db, container)

    still_there = await files_service.get_file(db, file_id=first.id, organization_id=org)
    assert still_there.size_bytes == len(b"second draft")


async def test_a_list_shows_one_file_per_path(db, org, bucket, container, client, headers):
    """Three hand-overs of one project are one file, not three with one name."""
    container.write("chart.png", b"first draft")
    await collect(db, container)
    container.write("chart.png", b"second draft")
    await collect(db, container)

    body = (await client.get("/v1/files?scope_id=ses_1", headers=headers)).json()

    assert [f["size_bytes"] for f in body["data"]] == [len(b"second draft")]


async def test_a_deleted_file_stops_being_listed(db, org, bucket, container, client, headers):
    """The listing is a directory, so a path the agent removed leaves it."""
    container.write("chart.png", b"a draft")
    container.write("keep.txt", b"still here")
    await collect(db, container)

    container.remove("chart.png")
    await collect(db, container)

    body = (await client.get("/v1/files?scope_id=ses_1", headers=headers)).json()
    assert [f["filename"] for f in body["data"]] == ["keep.txt"]


async def test_a_deleted_file_can_still_be_downloaded(db, org, bucket, container, client, headers):
    """Archiving is not deleting: whoever holds the id still gets the bytes."""
    container.write("chart.png", b"a draft")
    container.write("keep.txt", b"still here")
    taken = (await collect(db, container))[0]

    container.remove("chart.png")
    await collect(db, container)

    described = await client.get(f"/v1/files/{taken.id}", headers=headers)
    assert described.status_code == 200
    assert described.json()["archived"] is True
    content = await client.get(f"/v1/files/{taken.id}/content", headers=headers)
    assert content.status_code == 307


async def test_an_unreadable_container_retires_nothing(db, org, bucket, container):
    """An empty listing is how a container that could not be read answers, so
    it must not be read as an empty directory — that would retire every file
    the session made."""
    container.write("chart.png", b"a draft")
    await collect(db, container)

    container.clear()
    await collect(db, container)

    from app.db.queries import files as files_q

    live = await files_q.list_live_scoped_files(db, scope_id="ses_1", organization_id=org)
    assert [f.filename for f in live] == ["chart.png"]


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


async def test_taking_it_again_after_a_change_updates_the_record(client, headers, running, container):
    container.write("report.pdf", b"draft")
    first = (await take(client, headers, running, "report.pdf")).json()

    container.write("report.pdf", b"final version")
    second = (await take(client, headers, running, "report.pdf")).json()

    assert second["id"] == first["id"]
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


async def test_taking_from_a_session_with_no_sandbox_is_refused(
    client, headers, agent, environment, db, org
):
    """The file was in a container that no longer exists. Anything already
    collected is on `/v1/files` instead."""
    from app.db.queries import sessions as sessions_q

    session_id = (await attach(client, headers, agent, environment, [])).json()["id"]
    sandbox = await sessions_q.get_sandbox(
        db, session_id=session_id, organization_id=org
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


async def test_a_failed_sandbox_fetch_is_not_reported_as_an_attachment(
    db, org, bucket, client, headers, monkeypatch
):
    from types import SimpleNamespace

    from app.utils.sandbox import UPLOADS_DIR, Sandbox

    file_id = await upload(client, headers, bucket)

    async def _run(self, command, **kwargs):
        return SimpleNamespace(exit_code=22)

    monkeypatch.setattr(Sandbox, "run", _run)
    with pytest.raises(RuntimeError, match="could not fetch"):
        await Sandbox.from_id("sbx_1", "ses_1", org).upload_file(
            db, f"{UPLOADS_DIR}/sales.csv", file_id
        )


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


def test_langchain_e2b_calls_nothing_deepagents_moved():
    """`langchain-e2b` reaches into DeepAgents' private API. Hold it to it.

    It imports seven underscore-prefixed names from
    `deepagents.backends.sandbox` and subclasses `BaseSandbox` to inherit
    more, none of which carries a compatibility promise. DeepAgents 0.6.11
    turned `BaseSandbox._map_edit_error` into a module-level function while
    `AsyncE2BSandbox.aedit` went on calling `self._map_edit_error(...)`, and
    because `deepagents>=0.6.0,<0.7.0` admits both, the mismatch installs
    cleanly and only shows up when an edit fails — the one path that reaches
    the call. `app.utils.sandbox` puts that method back.

    Asserting the shape rather than that one name is what makes this worth
    keeping: every `self._x` either class resolves is checked, so the next
    private method to move is a failing test here rather than an
    `AttributeError` in front of the model. Attributes assigned on `self`
    are the classes' own, so they are excluded; each class is read alone,
    since the sync and async halves do not share a body.
    """
    import ast
    import inspect

    import app.utils.sandbox  # noqa: F401  — installs the patch under test
    import langchain_e2b.sandbox as module
    from langchain_e2b import AsyncE2BSandbox, E2BSandbox

    bodies = {
        node.name: node
        for node in ast.parse(inspect.getsource(module)).body
        if isinstance(node, ast.ClassDef)
    }

    unresolved: dict[str, list[str]] = {}
    for cls in (AsyncE2BSandbox, E2BSandbox):
        read: set[str] = set()
        written: set[str] = set()
        for node in ast.walk(bodies[cls.__name__]):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                (written if isinstance(node.ctx, ast.Store) else read).add(node.attr)
        missing = sorted(name for name in read - written if not hasattr(cls, name))
        if missing:
            unresolved[cls.__name__] = missing

    assert not unresolved, (
        f"langchain-e2b calls {unresolved} on itself and nothing defines them — "
        f"a DeepAgents private method moved again. Each one is an AttributeError "
        f"on whichever sandbox path reaches it; patch it in app/utils/sandbox.py "
        f"the way _map_edit_error is patched"
    )

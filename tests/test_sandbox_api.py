"""Containers a caller holds directly, through `/v1/sandbox`.

E2B is stubbed. What is being tested is everything this service decides: that
a caller's command is never spliced into a shell line, that output is bounded
before it reaches this process, that a command outliving its wait is still
findable afterwards, and that one tenant cannot reach another's container.

The shell itself — the redirection, `timeout`, base64 — is not exercised here
and cannot be. `infra/e2b/hf_lint/smoke.py` runs the real thing against a real
container.
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.sandbox import MAX_OUTPUT_CHARS
from app.routers.deps import get_db
from app.services import sandboxes as service


class FakeContainer:
    """One stand-in container, remembering everything asked of it.

    It does not run a shell. `run` recognises the collector by the report it
    asks for and answers from whatever the test set; everything else is
    recorded and returns success. That is enough to test the state machine and
    the parsing, which is all this layer owns.
    """

    registry: dict[str, "FakeContainer"] = {}

    def __init__(self, sandbox_id: str, scope_id: str, organization_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.scope_id = scope_id
        self.organization_id = organization_id
        self.commands: list[str] = []
        self.written: dict[str, bytes] = {}
        self.killed = False
        self.uploads: list[tuple[str, str]] = []
        # What the next collector call reports.
        self.rc: str = ""
        self.stdout: bytes = b""
        self.stderr: bytes = b""
        self.stdout_len: int | None = None
        self.started = "1000"
        self.ended = "1002"
        # Whether the exec directory is there. `get_result` on an id that was
        # never run has to come back as not found.
        self.found = True

    def finish(self, *, rc: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.rc = str(rc)
        self.stdout = stdout
        self.stderr = stderr

    async def run(self, command: str, **kwargs) -> object:
        self.commands.append(command)
        if "RC:" not in command:
            return _Result(0, "")
        out = self.stdout[:MAX_OUTPUT_CHARS]
        err = self.stderr[:MAX_OUTPUT_CHARS]
        report = "\n".join(
            [
                f"FOUND:{1 if self.found else 0}",
                f"RC:{self.rc}",
                f"STARTED:{self.started if self.rc else ''}",
                f"ENDED:{self.ended if self.rc else ''}",
                f"OUTLEN:{self.stdout_len if self.stdout_len is not None else len(self.stdout)}",
                f"ERRLEN:{len(self.stderr)}",
                f"OUT:{base64.b64encode(out).decode()}",
                f"ERR:{base64.b64encode(err).decode()}",
            ]
        )
        return _Result(0, report + "\n")

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.written[path] = data

    async def kill(self) -> None:
        self.killed = True

    async def upload_file(self, db, path: str, file_id: str) -> None:
        self.uploads.append((path, file_id))


class _Result:
    def __init__(self, exit_code: int, stdout: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture
def containers(monkeypatch):
    """Stand in for E2B, keeping the database side real."""

    FakeContainer.registry = {}
    started: list[dict] = []

    async def _start(*, image, scope_id, organization_id, ttl_seconds, network_access):
        started.append(
            {
                "image": image.image_id,
                "ttl_seconds": ttl_seconds,
                "network_access": network_access,
            }
        )
        sandbox_id = f"e2b-{len(started)}"
        container = FakeContainer(sandbox_id, scope_id, organization_id)
        FakeContainer.registry[sandbox_id] = container
        return container

    def _from_id(sandbox_id, scope_id, organization_id, ttl_seconds=None):
        return FakeContainer.registry.setdefault(
            sandbox_id, FakeContainer(sandbox_id, scope_id, organization_id)
        )

    monkeypatch.setattr(service.Container, "start", staticmethod(_start))
    monkeypatch.setattr(service.Container, "from_id", staticmethod(_from_id))
    FakeContainer.started = started
    return started


@pytest_asyncio.fixture
async def client(db, containers, builds):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http
    app.dependency_overrides.clear()


async def make(client, headers, **body):
    body.setdefault("system_environment", "hf-lint")
    response = await client.post("/v1/sandbox/create", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def container_for(sandbox: dict) -> FakeContainer:
    return next(
        c for c in FakeContainer.registry.values() if c.scope_id == sandbox["id"]
    )


# --- creating ---------------------------------------------------------------


async def test_a_sandbox_names_the_image_a_system_environment_ships(client, headers):
    sandbox = await make(client, headers)

    assert sandbox["state"] == "running"
    assert FakeContainer.started[0]["image"] == "votrix-hf-lint"


async def test_the_ttl_is_written_into_the_provider_not_just_the_row(client, headers):
    """It is what bounds a leak, so it has to be enforced where the container is."""

    sandbox = await make(client, headers, ttl_seconds=120)

    assert FakeContainer.started[0]["ttl_seconds"] == 120
    assert sandbox["ttl_seconds"] == 120


async def test_the_network_is_on_unless_turned_off(client, headers):
    """A sandbox that cannot reach the network cannot install anything.

    It also matches the containers agents run in, which have always had the
    network — it would be a strange asymmetry for a command someone wrote
    deliberately to be more restricted than one a model chose.
    """
    await make(client, headers)
    assert FakeContainer.started[0]["network_access"] is True

    await make(client, headers, network_access=False)
    assert FakeContainer.started[1]["network_access"] is False


async def test_a_system_environment_is_registered_once_and_reused(client, headers):
    first = await make(client, headers)
    second = await make(client, headers)

    assert first["environment_id"] == second["environment_id"]


async def test_an_unknown_system_environment_is_refused(client, headers):
    response = await client.post(
        "/v1/sandbox/create",
        headers=headers,
        json={"system_environment": "does-not-exist"},
    )
    assert response.status_code == 400
    assert "hf-lint" in response.text, "it should say what is available"


async def test_naming_neither_image_or_both_is_refused(client, headers):
    for body in ({}, {"environment_id": "env_1", "system_environment": "hf-lint"}):
        response = await client.post(
            "/v1/sandbox/create", headers=headers, json=body
        )
        assert response.status_code == 422, body


async def test_a_tenant_cannot_hold_more_than_the_cap(client, headers, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "max_sandboxes_per_organization", 2, raising=False
    )

    await make(client, headers)
    await make(client, headers)
    response = await client.post(
        "/v1/sandbox/create", headers=headers, json={"system_environment": "hf-lint"}
    )

    assert response.status_code == 409
    assert "limit of 2" in response.text


# --- exec -------------------------------------------------------------------


async def run(client, headers, sandbox, command="echo hi", **body):
    body.setdefault("wait_seconds", 0)
    response = await client.post(
        "/v1/sandbox/exec",
        headers=headers,
        json={"sandbox_id": sandbox["id"], "command": command, **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_command_is_never_part_of_a_shell_line(client, headers):
    """It travels base64-encoded and is decoded into a file inside the sandbox.

    A command carrying quotes, newlines or a `$(...)` is ordinary here — an
    agent writes them. Interpolating one into a launcher would make what runs
    depend on how it was quoted. Base64's alphabet has no shell metacharacter
    in it, which is what makes the encoded form safe to interpolate where the
    raw command is not.
    """
    import base64
    import re

    sandbox = await make(client, headers)
    nasty = "echo \"$(touch /tmp/pwned)\"; rm -rf / # '\n"

    await run(client, headers, sandbox, command=nasty)

    issued = container_for(sandbox).commands
    for line in issued:
        assert nasty not in line, "the raw command reached a shell line"

    encoded = re.findall(r"printf %s '([A-Za-z0-9+/=]+)'", issued[0])
    decoded = [base64.b64decode(chunk).decode() for chunk in encoded]
    assert nasty in decoded, "the command never reached the sandbox"


async def test_one_command_is_one_round_trip(client, headers):
    """Setting up, launching, waiting and reporting are one call.

    They were five. Each is a round trip to the provider — measured between
    0.9 and 2.5 seconds — so the four extra cost more than the work.
    """
    sandbox = await make(client, headers)
    before = len(container_for(sandbox).commands)

    await run(client, headers, sandbox)

    assert len(container_for(sandbox).commands) - before == 1


async def test_reading_a_result_is_also_one_round_trip(client, headers):
    """It used to probe for the directory first, then collect."""
    sandbox = await make(client, headers)
    pending = await run(client, headers, sandbox)
    container = container_for(sandbox)
    before = len(container.commands)

    await client.post(
        "/v1/sandbox/get_result",
        headers=headers,
        json={"sandbox_id": sandbox["id"], "exec_id": pending["id"]},
    )

    assert len(container.commands) - before == 1


async def test_a_result_for_an_exec_that_never_ran_is_not_found(client, headers):
    sandbox = await make(client, headers)
    container_for(sandbox).found = False

    response = await client.post(
        "/v1/sandbox/get_result",
        headers=headers,
        json={"sandbox_id": sandbox["id"], "exec_id": "exec_nope"},
    )

    assert response.status_code == 404


async def test_a_finished_command_reports_its_output_and_code(client, headers):
    sandbox = await make(client, headers)
    container = container_for(sandbox)
    container.finish(rc=0, stdout=b'{"ok": true}')

    result = await run(client, headers, sandbox)

    assert result["state"] == "succeeded"
    assert result["exit_code"] == 0
    assert result["stdout"] == '{"ok": true}'
    assert result["stdout_truncated"] is False
    assert result["duration_ms"] == 2000


async def test_a_nonzero_exit_is_failed_not_an_error(client, headers):
    """The command ran. What it decided is the caller's business, not ours."""
    sandbox = await make(client, headers)
    container_for(sandbox).finish(rc=1, stderr=b"boom")

    result = await run(client, headers, sandbox)

    assert result["state"] == "failed"
    assert result["exit_code"] == 1
    assert result["stderr"] == "boom"


async def test_a_command_killed_by_its_timeout_says_so(client, headers):
    sandbox = await make(client, headers)
    container_for(sandbox).finish(rc=124)

    result = await run(client, headers, sandbox, timeout_seconds=1)

    assert result["state"] == "timed_out"


async def test_output_is_bounded_before_it_reaches_this_process(client, headers):
    """The cap is applied in the container, and the caller is told it was.

    Capping the response instead would mean holding the whole thing first,
    which is the failure this exists to prevent.
    """
    sandbox = await make(client, headers)
    container = container_for(sandbox)
    container.finish(rc=0, stdout=b"x" * (MAX_OUTPUT_CHARS * 4))
    container.stdout_len = MAX_OUTPUT_CHARS * 4

    result = await run(client, headers, sandbox)

    assert len(result["stdout"]) == MAX_OUTPUT_CHARS
    assert result["stdout_truncated"] is True
    collector = container.commands[-1]
    assert f"head -c {MAX_OUTPUT_CHARS}" in collector


async def test_a_command_still_running_comes_back_running_and_is_found_later(
    client, headers
):
    """The wait expiring is not the command failing, and not the end of it."""
    sandbox = await make(client, headers)
    container = container_for(sandbox)

    pending = await run(client, headers, sandbox, command="sleep 300")
    assert pending["state"] == "running"
    assert pending["exit_code"] is None

    container.finish(rc=0, stdout=b"done")
    response = await client.post(
        "/v1/sandbox/get_result",
        headers=headers,
        json={"sandbox_id": sandbox["id"], "exec_id": pending["id"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
    assert response.json()["stdout"] == "done"
    assert response.json()["id"] == pending["id"]


async def test_two_commands_do_not_share_a_directory(client, headers):
    sandbox = await make(client, headers)

    first = await run(client, headers, sandbox)
    second = await run(client, headers, sandbox)

    assert first["dir"] != second["dir"]


async def test_the_wait_happens_in_the_container_not_here(client, headers):
    """One round trip, however long the wait — the sleeping is not ours."""
    sandbox = await make(client, headers)

    await run(client, headers, sandbox, wait_seconds=30)

    collector = container_for(sandbox).commands[-1]
    assert "deadline=" in collector and "sleep 0.2" in collector


# --- lifetime ---------------------------------------------------------------


async def test_deleting_kills_the_container_and_closes_the_row(client, headers):
    sandbox = await make(client, headers)
    container = container_for(sandbox)

    response = await client.post(
        "/v1/sandbox/delete", headers=headers, json={"sandbox_id": sandbox["id"]}
    )

    assert response.status_code == 200
    assert container.killed is True
    read = await client.post(
        "/v1/sandbox/get", headers=headers, json={"sandbox_id": sandbox["id"]}
    )
    assert read.json()["state"] == "terminated"


async def test_a_deleted_sandbox_refuses_work_rather_than_starting_another(
    client, headers
):
    sandbox = await make(client, headers)
    await client.post(
        "/v1/sandbox/delete", headers=headers, json={"sandbox_id": sandbox["id"]}
    )

    response = await client.post(
        "/v1/sandbox/exec",
        headers=headers,
        json={"sandbox_id": sandbox["id"], "command": "echo hi", "wait_seconds": 0},
    )

    assert response.status_code == 409
    assert "terminated" in response.text


async def test_a_sandbox_past_its_expiry_is_paused_not_gone(client, headers, db):
    """Nothing is collected on a timer.

    Past its timeout the provider pauses the container — the filesystem stays
    and the next call wakes it in about a second. Reading the row must not
    invent an ending the provider never performed.
    """

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db.models import Sandbox

    sandbox = await make(client, headers, ttl_seconds=30)
    row = (
        await db.execute(select(Sandbox).where(Sandbox.id == sandbox["id"]))
    ).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()

    read = await client.post(
        "/v1/sandbox/get", headers=headers, json={"sandbox_id": sandbox["id"]}
    )
    assert read.json()["state"] == "running"

    worked = await client.post(
        "/v1/sandbox/exec",
        headers=headers,
        json={"sandbox_id": sandbox["id"], "command": "echo hi", "wait_seconds": 0},
    )
    assert worked.status_code == 200, "a paused container wakes rather than refusing"


async def test_listing_shows_what_this_tenant_holds(client, headers):
    await make(client, headers)
    await make(client, headers)

    response = await client.post("/v1/sandbox/list", headers=headers, json={})

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 2


async def test_another_tenant_cannot_reach_this_one(client, headers, other_tenant):
    sandbox = await make(client, headers)
    _, other_headers = other_tenant

    for path, body in (
        ("get", {"sandbox_id": sandbox["id"]}),
        ("delete", {"sandbox_id": sandbox["id"]}),
        (
            "exec",
            {"sandbox_id": sandbox["id"], "command": "echo hi", "wait_seconds": 0},
        ),
    ):
        response = await client.post(
            f"/v1/sandbox/{path}", headers=other_headers, json=body
        )
        assert response.status_code == 404, path


# --- what a sandbox may reach ----------------------------------------------


@pytest.fixture
def e2b_create(monkeypatch):
    """Capture what `Sandbox.start` asks the provider for."""

    from app.utils import sandbox as sandbox_utils

    calls: list[dict] = []

    class _Native:
        sandbox_id = "e2b-native"

    async def _create(**kwargs):
        calls.append(kwargs)
        return _Native()

    monkeypatch.setattr(sandbox_utils.AsyncSandbox, "create", staticmethod(_create))
    monkeypatch.setattr(
        sandbox_utils.get_settings(),
        "s3_endpoint_url",
        "https://abc123.r2.cloudflarestorage.com",
        raising=False,
    )
    return calls


async def start(**overrides):
    from app.utils.sandbox import Image, Sandbox as Container

    return await Container.start(
        image=Image.base(),
        scope_id="sbx_1",
        organization_id="org_1",
        ttl_seconds=120,
        **overrides,
    )


async def test_a_restricted_sandbox_reaches_object_storage_and_nothing_else(e2b_create):
    """"No network" cannot mean no egress at all.

    Getting a file in or out is the sandbox fetching a signed URL from object
    storage. Cutting egress completely would not make it safer, it would make
    it useless — the only way left to hand it a file would be to stream the
    bytes through this service, which is what the signed URL exists to avoid.
    """
    await start(network_access=False)

    network = e2b_create[0]["network"]
    assert network["allow_out"] == ["abc123.r2.cloudflarestorage.com"]
    assert network["deny_out"] == ["0.0.0.0/0"], (
        "the provider treats an allow list without an explicit deny as allow-all"
    )


async def test_an_unrestricted_sandbox_is_left_alone(e2b_create):
    await start(network_access=True)

    assert e2b_create[0]["network"] is None


async def test_the_ttl_goes_to_the_provider(e2b_create):
    await start(network_access=False)

    assert e2b_create[0]["timeout"] == 120


# --- containers a conversation owns -----------------------------------------


@pytest_asyncio.fixture
async def session_sandbox(db, session):
    """A container owned by a conversation, written the way provisioning does."""

    from app.db.queries import sessions as sessions_q

    row = await sessions_q.create_sandbox(
        db,
        session,
        provider="e2b",
        external_sandbox_id="e2b-session",
        ttl_seconds=900,
        network_access=True,
    )
    await sessions_q.update_sandbox_state(db, row, state="running")
    await db.commit()
    return row


async def test_a_conversations_container_cannot_be_deleted_here(
    client, headers, session_sandbox
):
    """Killing it would end the conversation, and this verb does not say that.

    The next turn would find nothing to run in and terminate the session. A
    call named "delete a sandbox" must not be able to do that; deleting the
    session is the way, and it says so.
    """
    response = await client.post(
        "/v1/sandbox/delete",
        headers=headers,
        json={"sandbox_id": session_sandbox.id},
    )

    assert response.status_code == 409
    assert session_sandbox.session_id in response.text, "it should say which one"
    assert "delete the session instead" in response.text


async def test_a_conversations_container_can_still_be_worked_in(
    client, headers, session_sandbox
):
    """Same organization, same owner. Refusing would draw a line the data
    model no longer draws — and looking inside a stuck agent's container is
    the reason someone would reach for this."""

    response = await client.post(
        "/v1/sandbox/exec",
        headers=headers,
        json={
            "sandbox_id": session_sandbox.id,
            "command": "ls",
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 200, response.text


async def test_listing_leaves_out_what_the_caller_cannot_end(
    client, headers, session_sandbox
):
    await make(client, headers)

    default = await client.post("/v1/sandbox/list", headers=headers, json={})
    included = await client.post(
        "/v1/sandbox/list", headers=headers, json={"include_session_owned": True}
    )

    assert [row["session_id"] for row in default.json()["data"]] == [None]
    assert len(included.json()["data"]) == 2


async def test_the_limit_counts_containers_of_both_sorts(
    client, headers, session_sandbox, monkeypatch
):
    """The bug that made keeping two tables worth ending.

    The limit is about what an organization has running at the provider, and
    the provider does not care which of ours asked for it.
    """
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "max_sandboxes_per_organization", 2, raising=False
    )

    await make(client, headers)  # one API-held, plus the conversation's = 2
    response = await client.post(
        "/v1/sandbox/create", headers=headers, json={"system_environment": "hf-lint"}
    )

    assert response.status_code == 409
    assert "limit of 2" in response.text


# --- ending a conversation ends its container -------------------------------


async def test_deleting_a_session_kills_its_container(db, session, monkeypatch):
    """Otherwise the container outlives every record of its own id.

    Deleting the session cascades its row away, and that row holds the only
    copy of the provider's id — so without this the container stays paused at
    the provider forever with nothing left that can name it. Several hundred
    of ours went exactly that way.
    """
    from app.db.queries import sessions as sessions_q
    from app.services import sessions as sessions_service
    from app.utils import sandbox as sandbox_utils

    killed: list[str] = []

    class _Handle:
        def __init__(self, sandbox_id, *_a, **_k):
            self._id = sandbox_id

        async def kill(self):
            killed.append(self._id)

    monkeypatch.setattr(sandbox_utils.Sandbox, "from_id", staticmethod(_Handle))

    row = await sessions_q.create_sandbox(
        db,
        session,
        provider="e2b",
        external_sandbox_id="e2b-doomed",
        ttl_seconds=900,
        network_access=True,
    )
    await sessions_q.update_sandbox_state(db, row, state="running")
    await db.commit()

    await sessions_service.delete_session(
        db, session_id=session.id, organization_id=session.organization_id
    )

    assert killed == ["e2b-doomed"]


async def test_a_container_that_will_not_die_does_not_trap_the_session(
    db, session, monkeypatch
):
    """The row goes either way, so refusing would leave a session nobody can
    get rid of."""

    from app.db.queries import sessions as sessions_q
    from app.services import sessions as sessions_service
    from app.utils import sandbox as sandbox_utils

    class _Stubborn:
        def __init__(self, *_a, **_k):
            pass

        async def kill(self):
            raise RuntimeError("provider is down")

    monkeypatch.setattr(sandbox_utils.Sandbox, "from_id", staticmethod(_Stubborn))

    row = await sessions_q.create_sandbox(
        db,
        session,
        provider="e2b",
        external_sandbox_id="e2b-stuck",
        ttl_seconds=900,
        network_access=True,
    )
    await sessions_q.update_sandbox_state(db, row, state="running")
    await db.commit()

    deleted = await sessions_service.delete_session(
        db, session_id=session.id, organization_id=session.organization_id
    )

    assert deleted.id == session.id

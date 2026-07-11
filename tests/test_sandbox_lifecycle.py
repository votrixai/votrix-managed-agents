from __future__ import annotations

import pytest
from deepagents.backends import StateBackend

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import agents as agents_q
from app.db.queries import environments as environments_q
from app.db.queries import session_sandboxes as sandboxes_q
from app.db.queries import sessions as sessions_q
from app.runtime.sandbox_inputs import SandboxInputBundle, SandboxInputError, SandboxInputFile, sandbox_input_bundle
from app.runtime.sandbox import open_backend
from app.runtime.sandbox_lifecycle import (
    SandboxInputMismatchError,
    SandboxLifecycleConfigurationError,
    open_e2b_session_backend,
    provision_session_sandbox,
    sandbox_policy_from_environment,
)
from app.runtime.sandbox_providers import (
    SandboxConnection,
    SandboxOwner,
    SandboxPolicy,
    SandboxProviderCapabilities,
    SandboxReference,
)
from tests.conftest import TEST_HEADERS


class FakeLifecycleProvider:
    name = "e2b"
    capabilities = SandboxProviderCapabilities(
        execute=True,
        file_transfer=True,
        persistence=True,
        pause=True,
        network_modes=frozenset({"none", "limited", "unrestricted"}),
        secure_control_plane=True,
    )

    def __init__(self) -> None:
        self.external_id = "opaque-e2b-id"
        self.provision_count = 0
        self.bootstrap_count = 0
        self.connect_count = 0
        self.pause_count = 0
        self.delete_count = 0
        self.verify_count = 0
        self.uploads: list[list[tuple[str, bytes]]] = []
        self.sealed_digest = ""

    def _connection(
        self,
        owner: SandboxOwner,
        policy: SandboxPolicy,
        *,
        state: str,
    ) -> SandboxConnection:
        resolved = policy.resolved(
            default_timeout_seconds=900,
            default_command_timeout_seconds=900,
        )
        reference = SandboxReference(
            provider="e2b",
            external_id=self.external_id,
            owner_fingerprint=owner.fingerprint,
            policy_fingerprint=resolved.fingerprint,
            template_id="base",
        )
        return SandboxConnection(
            reference=reference,
            backend=StateBackend(),
            config={
                **reference.to_config(),
                "policy": resolved.to_dict(),
                "keep_memory": True,
            },
            capabilities=self.capabilities.to_dict(),
            metadata={"provider": "e2b", "state": state},
        )

    async def provision(self, owner, policy, *, template=None):
        self.provision_count += 1
        return self._connection(owner, policy, state="running")

    async def bootstrap(
        self,
        connection,
        *,
        files,
        read_only_paths,
        mutable_roots,
        digest,
    ):
        self.bootstrap_count += 1
        self.uploads.append(list(files))
        self.sealed_digest = digest

    async def connect(self, reference, owner, policy):
        self.connect_count += 1
        assert reference.external_id == self.external_id
        return self._connection(owner, policy, state="running")

    async def verify_bootstrap(self, connection, *, digest, immutable_manifest):
        self.verify_count += 1
        assert digest == self.sealed_digest
        assert isinstance(immutable_manifest, dict)

    async def pause(self, reference, owner):
        self.pause_count += 1

    async def delete(self, reference, owner):
        self.delete_count += 1


async def _managed_session(*, workspace_id: str):
    async with session_scope() as db:
        agent, version = await agents_q.create_agent(
            db,
            name="E2B lifecycle",
            model={"id": "claude-sonnet-4-6"},
            workspace_id=workspace_id,
        )
        environment = await environments_q.create_environment(
            db,
            name="e2b-lifecycle",
            config={"type": "cloud", "networking": {"type": "none"}},
            workspace_id=workspace_id,
        )
        session = await sessions_q.create_session(
            db,
            agent=agent,
            agent_version=version.version,
            environment=environment,
            workspace_id=workspace_id,
        )
        await db.commit()
        return session.id, environment.config, version


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "e2b")
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setenv("VMA_E2B_WORKDIR", "/workspace")
    monkeypatch.setenv("VMA_E2B_TEMPLATE", "base")
    monkeypatch.setenv("VMA_E2B_PAUSE_ON_EXIT", "true")
    get_settings.cache_clear()


def test_read_write_memory_seed_is_part_of_fixed_input_identity():
    first = SandboxInputBundle(
        files=(
            SandboxInputFile(
                path="/mnt/memory/preferences/profile.md",
                content=b"first seed",
                read_only=False,
                source="memory_seed",
            ),
        ),
        skill_sources=(),
        memory_sources=("/mnt/memory/preferences/AGENTS.md",),
        mutable_roots=("/mnt/memory/preferences",),
    )
    changed = SandboxInputBundle(
        files=(
            SandboxInputFile(
                path="/mnt/memory/preferences/profile.md",
                content=b"changed at the control plane",
                read_only=False,
                source="memory_seed",
            ),
        ),
        skill_sources=first.skill_sources,
        memory_sources=first.memory_sources,
        mutable_roots=first.mutable_roots,
    )

    assert first.input_digest != changed.input_digest
    assert first.immutable_manifest == changed.immutable_manifest == {}


def test_e2b_rejects_unsupported_inputs_without_changing_other_backends():
    context = {"session_resource_types": ["github_repository"]}

    assert sandbox_input_bundle(context).files == ()
    with pytest.raises(SandboxInputError, match="github_repository"):
        sandbox_input_bundle(context, reject_unsupported_resources=True)


def test_e2b_requires_named_hardened_template(monkeypatch):
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "e2b")
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setenv("VMA_E2B_TEMPLATE", "")
    get_settings.cache_clear()

    with pytest.raises(SandboxLifecycleConfigurationError, match="hardened template"):
        sandbox_policy_from_environment(
            {"type": "cloud", "networking": {"type": "none"}}
        )


async def test_one_session_provisions_once_and_turns_only_reconnect(monkeypatch):
    import app.runtime.sandbox_lifecycle as lifecycle

    _configure(monkeypatch)
    provider = FakeLifecycleProvider()
    monkeypatch.setattr(lifecycle, "build_e2b_provider", lambda: provider)
    workspace_id = "wrkspc_e2b_single"
    session_id, config, version = await _managed_session(workspace_id=workspace_id)

    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_id, workspace_id=workspace_id)
        assert session is not None
        assert await provision_session_sandbox(
            db,
            session=session,
            agent_version=version,
            environment_config=config,
        )
        await db.commit()

    assert provider.provision_count == 1
    assert provider.bootstrap_count == 1
    assert provider.pause_count == 1
    assert provider.uploads == [[]]

    empty_bundle = SandboxInputBundle(
        files=(),
        skill_sources=(),
        memory_sources=(),
        mutable_roots=(),
    )
    async with open_e2b_session_backend(
        workspace_id=workspace_id,
        session_id=session_id,
        environment_config=config,
        input_bundle=empty_bundle,
    ) as first:
        assert first.reference.external_id == provider.external_id
    async with open_e2b_session_backend(
        workspace_id=workspace_id,
        session_id=session_id,
        environment_config=config,
        input_bundle=empty_bundle,
    ) as second:
        assert second.reference.external_id == provider.external_id

    assert provider.provision_count == 1
    assert provider.bootstrap_count == 1
    assert provider.connect_count == 2
    assert provider.verify_count == 2
    assert provider.pause_count == 3
    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
        )
        assert record is not None
        assert record.external_sandbox_id == provider.external_id
        assert record.state == "paused"


async def test_changed_immutable_inputs_require_a_new_session(monkeypatch):
    import app.runtime.sandbox_lifecycle as lifecycle

    _configure(monkeypatch)
    provider = FakeLifecycleProvider()
    monkeypatch.setattr(lifecycle, "build_e2b_provider", lambda: provider)
    workspace_id = "wrkspc_e2b_mismatch"
    session_id, config, version = await _managed_session(workspace_id=workspace_id)

    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_id, workspace_id=workspace_id)
        assert session is not None
        await provision_session_sandbox(
            db,
            session=session,
            agent_version=version,
            environment_config=config,
        )
        await db.commit()

    changed = SandboxInputBundle(
        files=(
            SandboxInputFile(
                path="/mnt/session/inputs/changed.txt",
                content=b"changed",
                read_only=True,
                source="session_file",
            ),
        ),
        skill_sources=(),
        memory_sources=(),
        mutable_roots=(),
    )
    with pytest.raises(SandboxInputMismatchError, match="create a new Session"):
        async with open_e2b_session_backend(
            workspace_id=workspace_id,
            session_id=session_id,
            environment_config=config,
            input_bundle=changed,
        ):
            pass
    assert provider.connect_count == 0
    assert provider.bootstrap_count == 1


async def test_persisted_e2b_binding_cannot_silently_downgrade(monkeypatch):
    import app.runtime.sandbox_lifecycle as lifecycle

    _configure(monkeypatch)
    provider = FakeLifecycleProvider()
    monkeypatch.setattr(lifecycle, "build_e2b_provider", lambda: provider)
    workspace_id = "wrkspc_e2b_no_downgrade"
    session_id, config, version = await _managed_session(workspace_id=workspace_id)

    async with session_scope() as db:
        session = await sessions_q.get_session(db, session_id, workspace_id=workspace_id)
        assert session is not None
        await provision_session_sandbox(
            db,
            session=session,
            agent_version=version,
            environment_config=config,
        )
        await db.commit()

    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "state")
    get_settings.cache_clear()
    empty_bundle = SandboxInputBundle(
        files=(),
        skill_sources=(),
        memory_sources=(),
        mutable_roots=(),
    )
    changed_environment = {"type": "self_hosted", "networking": {"type": "none"}}
    async with open_backend(
        workspace_id=workspace_id,
        session_id=session_id,
        environment_config=changed_environment,
        input_bundle=empty_bundle,
    ) as handle:
        assert handle.plan.backend == "e2b"
        assert handle.backend is not None

    assert provider.connect_count == 1
    assert provider.bootstrap_count == 1


async def test_session_api_provisions_at_create_and_seals_resource_mutations(
    client,
    monkeypatch,
):
    import app.runtime.sandbox_lifecycle as lifecycle

    _configure(monkeypatch)
    provider = FakeLifecycleProvider()
    monkeypatch.setattr(lifecycle, "build_e2b_provider", lambda: provider)

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "E2B API Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={
            "name": "E2B API Environment",
            "config": {"type": "cloud", "networking": {"type": "none"}},
        },
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
    assert session["sandbox_state"]["persistence"] == "one_sandbox_per_session"
    assert "external_sandbox_id" not in session["sandbox_state"]
    assert provider.provision_count == 1
    assert provider.bootstrap_count == 1

    response = await client.post(
        f"/v1/sessions/{session['id']}/resources",
        headers=TEST_HEADERS,
        json={"type": "file", "file_id": "file_new"},
    )
    assert response.status_code == 409
    assert "sealed" in response.json()["error"]["message"]

    response = await client.delete(
        f"/v1/sessions/{session['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert provider.delete_count == 1


async def test_e2b_session_create_rejects_github_resource(client, monkeypatch):
    import app.runtime.sandbox_lifecycle as lifecycle

    _configure(monkeypatch)
    provider = FakeLifecycleProvider()
    monkeypatch.setattr(lifecycle, "build_e2b_provider", lambda: provider)

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "No GitHub E2B", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={
            "name": "No GitHub E2B Environment",
            "config": {"type": "cloud", "networking": {"type": "none"}},
        },
    )
    assert response.status_code == 201, response.text

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": agent["id"],
            "environment_id": response.json()["id"],
            "resources": [
                {
                    "type": "github_repository",
                    "url": "https://github.com/example/private-repo.git",
                    "authorization_token": "test-only-token",
                }
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert "github_repository" in response.json()["error"]["message"]
    assert provider.provision_count == 0

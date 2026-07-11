from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from app.runtime.sandbox_providers import (
    E2BDependencies,
    E2BSandboxProvider,
    SandboxDependencyError,
    SandboxNotFoundError,
    SandboxOperationError,
    SandboxOwner,
    SandboxOwnershipError,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxProviderCapabilities,
    SandboxReference,
)


class FakeNotFoundError(Exception):
    pass


class FakeNativeSandbox:
    def __init__(
        self,
        sandbox_id: str,
        *,
        metadata: dict[str, str],
        template: str | None,
        allow_internet_access: bool,
        network: dict[str, Any],
        lifecycle: dict[str, Any],
    ) -> None:
        self.sandbox_id = sandbox_id
        self.metadata = dict(metadata)
        self.template_id = template or "base"
        self.allow_internet_access = allow_internet_access
        self.network = dict(network)
        timeout_action = lifecycle["on_timeout"]
        self.lifecycle = {
            "on_timeout": (
                timeout_action["action"] if isinstance(timeout_action, dict) else timeout_action
            ),
            "auto_resume": lifecycle["auto_resume"],
        }
        self.state = "running"
        self.kill_calls: list[dict[str, Any]] = []
        self.command_exit_code = 0
        self.root_commands: list[dict[str, Any]] = []
        self.root_writes: list[dict[str, Any]] = []
        self.commands = _FakeCommands(self)
        self.files = _FakeFiles(self)

    async def get_info(self, **kwargs):
        return SimpleNamespace(
            sandbox_id=self.sandbox_id,
            template_id=self.template_id,
            metadata=dict(self.metadata),
            state=self.state,
            allow_internet_access=self.allow_internet_access,
            network=dict(self.network),
            lifecycle=dict(self.lifecycle),
        )

    async def kill(self, **kwargs):
        self.kill_calls.append(dict(kwargs))
        FakeSDK.registry.pop(self.sandbox_id, None)
        self.state = "killed"
        return True


class _FakeCommands:
    def __init__(self, sandbox: FakeNativeSandbox) -> None:
        self.sandbox = sandbox

    async def run(self, command: str, **kwargs):
        self.sandbox.root_commands.append({"command": command, **kwargs})
        return SimpleNamespace(
            exit_code=self.sandbox.command_exit_code,
            stdout="",
            stderr="",
        )


class _FakeFiles:
    def __init__(self, sandbox: FakeNativeSandbox) -> None:
        self.sandbox = sandbox

    async def write(self, path: str, content: bytes, **kwargs):
        self.sandbox.root_writes.append(
            {"path": path, "content": content, **kwargs}
        )
        return SimpleNamespace(path=path)


class FakeSDK:
    create_calls: list[dict[str, Any]] = []
    connect_calls: list[tuple[str, dict[str, Any]]] = []
    get_info_calls: list[tuple[str, dict[str, Any]]] = []
    pause_calls: list[tuple[str, dict[str, Any]]] = []
    kill_calls: list[tuple[str, dict[str, Any]]] = []
    registry: dict[str, FakeNativeSandbox] = {}
    next_id = "opaque-provider-id/with:punctuation"

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.connect_calls = []
        cls.get_info_calls = []
        cls.pause_calls = []
        cls.kill_calls = []
        cls.registry = {}

    @classmethod
    async def create(cls, **kwargs):
        cls.create_calls.append(dict(kwargs))
        native = FakeNativeSandbox(
            cls.next_id,
            metadata=kwargs["metadata"],
            template=kwargs.get("template"),
            allow_internet_access=kwargs["allow_internet_access"],
            network=kwargs["network"],
            lifecycle=kwargs["lifecycle"],
        )
        cls.registry[native.sandbox_id] = native
        return native

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs):
        cls.connect_calls.append((sandbox_id, dict(kwargs)))
        try:
            native = cls.registry[sandbox_id]
        except KeyError as exc:
            raise FakeNotFoundError(sandbox_id) from exc
        native.state = "running"
        return native

    @classmethod
    async def get_info(cls, sandbox_id: str, **kwargs):
        cls.get_info_calls.append((sandbox_id, dict(kwargs)))
        try:
            native = cls.registry[sandbox_id]
        except KeyError as exc:
            raise FakeNotFoundError(sandbox_id) from exc
        return await native.get_info()

    @classmethod
    async def pause(cls, sandbox_id: str, **kwargs):
        cls.pause_calls.append((sandbox_id, dict(kwargs)))
        try:
            native = cls.registry[sandbox_id]
        except KeyError as exc:
            raise FakeNotFoundError(sandbox_id) from exc
        if native.state == "paused":
            return False
        native.state = "paused"
        return True

    @classmethod
    async def kill(cls, sandbox_id: str, **kwargs):
        cls.kill_calls.append((sandbox_id, dict(kwargs)))
        native = cls.registry.pop(sandbox_id, None)
        if native is None:
            return False
        native.state = "killed"
        return True


class FakeBackend(SandboxBackendProtocol):
    def __init__(self, *, sandbox: FakeNativeSandbox, workdir: str, timeout: int) -> None:
        self.sandbox = sandbox
        self.workdir = workdir
        self.timeout = timeout

    @property
    def id(self) -> str:
        return self.sandbox.sandbox_id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return ExecuteResponse(output=command, exit_code=0)


@pytest.fixture(autouse=True)
def reset_fake_sdk():
    FakeSDK.reset()


def dependencies() -> E2BDependencies:
    return E2BDependencies(
        sandbox_class=FakeSDK,
        backend_class=FakeBackend,
        not_found_errors=(FakeNotFoundError,),
    )


def provider(*, loader=None) -> E2BSandboxProvider:
    return E2BSandboxProvider(
        "test-api-key",
        domain="sandboxes.example.test",
        api_url="https://control.example.test",
        sandbox_url="https://runtime.example.test",
        default_template="vma-python",
        timeout=900,
        command_timeout=300,
        dependencies=loader or dependencies,
    )


async def test_provision_returns_async_deepagents_backend_and_safe_record():
    owner = SandboxOwner("wrkspc_private", "session_private")
    policy = SandboxPolicy(
        network_access="limited",
        allowed_egress=("api.example.com", "*.files.example.com"),
        workdir="/workspace",
    )

    connection = await provider().provision(owner, policy)

    assert connection.external_id == FakeSDK.next_id
    assert connection.reference.template_id == "vma-python"
    assert isinstance(connection.backend, SandboxBackendProtocol)
    assert connection.backend.workdir == "/workspace"
    assert connection.backend.timeout == 300
    assert connection.backend.sandbox.sandbox_id == connection.external_id

    create = FakeSDK.create_calls[0]
    assert create["template"] == "vma-python"
    assert create["timeout"] == 900
    assert create["secure"] is True
    assert create["lifecycle"] == {
        "on_timeout": {"action": "pause", "keep_memory": True},
        "auto_resume": False,
    }
    assert create["allow_internet_access"] is True
    assert create["network"] == {
        "allow_public_traffic": False,
        "allow_out": ["*.files.example.com", "api.example.com"],
    }
    assert "deny_out" not in create["network"]
    assert create["api_key"] == "test-api-key"
    assert create["domain"] == "sandboxes.example.test"
    assert create["api_url"] == "https://control.example.test"
    assert create["sandbox_url"] == "https://runtime.example.test"
    assert "wrkspc_private" not in repr(create["metadata"])
    assert "session_private" not in repr(create["metadata"])

    assert connection.config["policy"]["network_access"] == "limited"
    assert connection.config["keep_memory"] is True
    assert connection.config["owner_fingerprint"] == owner.fingerprint
    assert "api_key" not in connection.config
    assert connection.capabilities["execute"] is True
    assert connection.metadata == {"provider": "e2b", "state": "running"}
    assert connection.to_record() == {
        "provider": "e2b",
        "external_sandbox_id": FakeSDK.next_id,
        "template_id": "vma-python",
        "state": "running",
        "config": connection.config,
        "capabilities": connection.capabilities,
    }
    assert "generation" not in connection.to_record()
    assert "snapshot_id" not in connection.to_record()
    assert "bootstrap_digest" not in connection.to_record()


@pytest.mark.parametrize(
    ("policy", "allow_internet"),
    [
        (SandboxPolicy(), False),
        (SandboxPolicy(network_access="unrestricted"), True),
    ],
)
async def test_network_modes_always_disable_public_traffic(policy, allow_internet):
    await provider().provision(SandboxOwner("wrkspc_a", "session_a"), policy)

    create = FakeSDK.create_calls[0]
    assert create["allow_internet_access"] is allow_internet
    assert create["network"] == {"allow_public_traffic": False}
    assert create["lifecycle"]["auto_resume"] is False


async def test_connect_checks_owner_and_policy_before_touching_e2b():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    policy = SandboxPolicy()
    created = await instance.provision(owner, policy)

    with pytest.raises(SandboxOwnershipError):
        await instance.connect(created.reference, SandboxOwner("wrkspc_b", "session_a"), policy)
    with pytest.raises(SandboxPolicyError):
        await instance.connect(
            created.reference,
            owner,
            SandboxPolicy(network_access="unrestricted"),
        )
    assert FakeSDK.get_info_calls == []
    assert FakeSDK.connect_calls == []

    connected = await instance.connect(created.reference, owner, policy)
    assert connected.external_id == created.external_id
    assert connected.backend.sandbox.native is FakeSDK.registry[created.external_id]
    assert FakeSDK.get_info_calls[0][0] == created.external_id
    assert FakeSDK.connect_calls == [
        (
            created.external_id,
            {
                "timeout": 900,
                "api_key": "test-api-key",
                "domain": "sandboxes.example.test",
                "api_url": "https://control.example.test",
                "sandbox_url": "https://runtime.example.test",
            },
        )
    ]


async def test_remote_metadata_mismatch_fails_before_resume_or_mutation():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    policy = SandboxPolicy()
    created = await instance.provision(owner, policy)
    native = FakeSDK.registry[created.external_id]
    native.metadata["vma_owner_fingerprint"] = "f" * 64

    with pytest.raises(SandboxOwnershipError):
        await instance.connect(created.reference, owner, policy)
    with pytest.raises(SandboxOwnershipError):
        await instance.pause(created.reference, owner)
    with pytest.raises(SandboxOwnershipError):
        await instance.delete(created.reference, owner)

    assert FakeSDK.connect_calls == []
    assert FakeSDK.pause_calls == []
    assert FakeSDK.kill_calls == []


async def test_pause_and_delete_use_static_calls_without_resuming():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    created = await instance.provision(owner, SandboxPolicy())

    await instance.pause(created.reference, owner)
    await instance.pause(created.reference, owner)
    assert FakeSDK.connect_calls == []
    assert FakeSDK.pause_calls == [
        (created.external_id, {"keep_memory": True, **instance._api_options()}),
        (created.external_id, {"keep_memory": True, **instance._api_options()}),
    ]

    await instance.delete(created.reference, owner)
    assert FakeSDK.connect_calls == []
    assert FakeSDK.kill_calls == [(created.external_id, instance._api_options())]
    assert created.external_id not in FakeSDK.registry


async def test_bootstrap_uploads_once_as_root_and_verifies_seal():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    created = await instance.provision(
        owner,
        SandboxPolicy(network_access="none", workdir="/workspace"),
    )
    digest = "sha256:" + "a" * 64

    await instance.bootstrap(
        created,
        files=[
            ("/mnt/session/inputs/data.txt", b"fixed"),
            ("/workspace/memory.txt", b"mutable"),
        ],
        read_only_paths=("/mnt/session/inputs/data.txt",),
        mutable_roots=("/workspace",),
        digest=digest,
    )
    await instance.verify_bootstrap(
        created,
        digest=digest,
        immutable_manifest={
            "/mnt/session/inputs/data.txt": (
                "992a93455c71fedd36ac9bbc439952c041cf61445958472af479269b8d873513"
            )
        },
    )

    native = FakeSDK.registry[created.external_id]
    assert native.root_writes == [
        {
            "path": "/mnt/session/inputs/data.txt",
            "content": b"fixed",
            "user": "root",
        },
        {
            "path": "/workspace/memory.txt",
            "content": b"mutable",
            "user": "root",
        },
    ]
    assert len(native.root_commands) == 5
    guest_attestations = [call for call in native.root_commands if "user" not in call]
    root_operations = [call for call in native.root_commands if call.get("user") == "root"]
    assert len(guest_attestations) == 2
    assert len(root_operations) == 3
    assert all(call["envs"] == {"VMA_EXPECTED_GUEST": "user"} for call in guest_attestations)
    assert all(call["timeout"] == 300 for call in native.root_commands)


async def test_bootstrap_rejects_read_only_content_inside_mutable_root():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    created = await instance.provision(owner, SandboxPolicy(workdir="/workspace"))

    with pytest.raises(SandboxPolicyError, match="overlap"):
        await instance.bootstrap(
            created,
            files=[("/workspace/input.txt", b"fixed")],
            read_only_paths=("/workspace/input.txt",),
            mutable_roots=("/workspace",),
            digest="sha256:" + "b" * 64,
        )
    assert FakeSDK.registry[created.external_id].root_writes == []


async def test_guest_execution_is_bound_and_unsafe_template_fails_closed():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    created = await instance.provision(owner, SandboxPolicy(workdir="/workspace"))
    native = FakeSDK.registry[created.external_id]

    await created.backend.sandbox.commands.run("whoami")
    await created.backend.sandbox.files.write("/workspace/guest.txt", b"guest")
    assert native.root_commands[-1]["user"] == "user"
    assert native.root_writes[-1]["user"] == "user"

    native.command_exit_code = 41
    with pytest.raises(SandboxPolicyError, match="privilege boundary"):
        await instance.bootstrap(
            created,
            files=[],
            read_only_paths=(),
            mutable_roots=("/workspace",),
            digest="sha256:" + "c" * 64,
        )


async def test_missing_sandbox_and_sdk_failures_are_safe_provider_errors():
    instance = provider()
    owner = SandboxOwner("wrkspc_a", "session_a")
    policy = SandboxPolicy()
    resolved = policy.resolved(default_timeout_seconds=900, default_command_timeout_seconds=300)
    reference = SandboxReference(
        provider="e2b",
        external_id="missing-secret-id",
        owner_fingerprint=owner.fingerprint,
        policy_fingerprint=resolved.fingerprint,
    )

    with pytest.raises(SandboxNotFoundError, match="not found") as caught:
        await instance.connect(reference, owner, policy)
    assert "missing-secret-id" not in str(caught.value)

    class BrokenSDK(FakeSDK):
        @classmethod
        async def create(cls, **kwargs):
            raise RuntimeError("provider secret response")

    broken = provider(
        loader=lambda: E2BDependencies(
            sandbox_class=BrokenSDK,
            backend_class=FakeBackend,
            not_found_errors=(FakeNotFoundError,),
        )
    )
    with pytest.raises(SandboxOperationError, match="provision failed") as failed:
        await broken.provision(owner, policy)
    assert "provider secret response" not in str(failed.value)


async def test_optional_dependencies_are_lazy_and_adapter_is_validated():
    calls = 0

    def lazy_loader():
        nonlocal calls
        calls += 1
        return dependencies()

    instance = provider(loader=lazy_loader)
    assert calls == 0
    await instance.provision(SandboxOwner("wrkspc_a", "session_a"), SandboxPolicy())
    assert calls == 1

    invalid = provider(
        loader=lambda: E2BDependencies(sandbox_class=object, backend_class=FakeBackend)
    )
    with pytest.raises(SandboxDependencyError, match="missing Sandbox.create"):
        await invalid.provision(SandboxOwner("wrkspc_a", "session_a"), SandboxPolicy())


def test_real_dependency_loader_selects_true_async_sdk_and_adapter():
    from app.runtime.sandbox_providers.e2b import load_e2b_dependencies

    bindings = load_e2b_dependencies()
    assert bindings.sandbox_class.__name__ == "AsyncSandbox"
    assert bindings.backend_class.__name__ == "AsyncE2BSandbox"


def test_policy_reference_and_capability_validation_are_fail_closed():
    with pytest.raises(SandboxPolicyError, match="requires allowed_egress"):
        SandboxPolicy(network_access="limited")
    with pytest.raises(SandboxPolicyError, match="only valid with limited"):
        SandboxPolicy(network_access="none", allowed_egress=("api.example.com",))
    with pytest.raises(SandboxPolicyError, match="Invalid egress"):
        SandboxPolicy(network_access="limited", allowed_egress=("https://api.example.com",))
    with pytest.raises(SandboxPolicyError, match="normalized absolute"):
        SandboxPolicy(workdir="/workspace/../escape")

    owner = SandboxOwner("wrkspc_a", "session_a")
    policy = SandboxPolicy().resolved(
        default_timeout_seconds=900,
        default_command_timeout_seconds=300,
    )
    reference = SandboxReference(
        provider="e2b",
        external_id=FakeSDK.next_id,
        owner_fingerprint=owner.fingerprint,
        policy_fingerprint=policy.fingerprint,
        template_id="vma-python",
    )
    assert reference.to_config() == {
        "owner_fingerprint": owner.fingerprint,
        "policy_fingerprint": policy.fingerprint,
    }
    assert not hasattr(reference, "generation")
    assert not hasattr(reference, "snapshot_id")

    capabilities = SandboxProviderCapabilities(
        execute=False,
        file_transfer=True,
        persistence=True,
        pause=True,
        network_modes=frozenset({"none"}),
        secure_control_plane=True,
    )
    with pytest.raises(SandboxPolicyError, match="execute"):
        capabilities.validate(policy)


def test_constructor_enforces_persistent_secure_configuration():
    with pytest.raises(ValueError, match="api_key"):
        E2BSandboxProvider("")
    with pytest.raises(ValueError, match="keep_memory=True"):
        E2BSandboxProvider("key", keep_memory=False)
    with pytest.raises(ValueError, match="non-root"):
        E2BSandboxProvider("key", guest_user="root")
    with pytest.raises(ValueError, match="domain"):
        E2BSandboxProvider("key", domain="https://example.com")
    with pytest.raises(ValueError, match="credentials"):
        E2BSandboxProvider("key", api_url="https://user:pass@example.com")

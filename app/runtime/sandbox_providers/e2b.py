"""E2B lifecycle provider backed by the official async adapter.

E2B and ``langchain-e2b`` are optional dependencies. VMA owns lifecycle and
authorization: provider auto-resume is disabled and every reconnect is checked
against the persisted session owner and policy fingerprints.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import urlsplit

from app.runtime.sandbox_providers.base import (
    ResolvedSandboxPolicy,
    SandboxConnection,
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

DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30 * 60
_PROVIDER = "e2b"
_OWNER_METADATA_KEY = "vma_owner_fingerprint"
_POLICY_METADATA_KEY = "vma_policy_fingerprint"
_MANAGED_BY_METADATA_KEY = "vma_managed_by"
_MANAGED_BY = "votrix-managed-agents"
_GUEST_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_SEAL_PATH = "/var/lib/vma/session-inputs.json"

_GUEST_ATTEST_COMMAND = """
set -eu
test "$(id -un)" = "$VMA_EXPECTED_GUEST"
test "$(id -u)" -ne 0
if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    exit 41
fi
""".strip()

_PREPARE_SCRIPT = """
import json, os, pwd, stat, sys
p = json.loads(sys.argv[1])
guest = pwd.getpwnam(p["guest"])
if guest.pw_uid == 0:
    raise SystemExit("guest must not be root")
def directory(path, mode=0o755, uid=0, gid=0):
    if not path.startswith("/") or os.path.normpath(path) != path or path == "/":
        raise SystemExit("unsafe directory")
    current = "/"
    for part in path.strip("/").split("/"):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("managed path is not a directory")
    os.chown(path, uid, gid)
    os.chmod(path, mode)
for root in p["protected_roots"]:
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("protected root is unsafe")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("protected root is not operator owned")
    if next(os.scandir(root), None) is not None:
        raise SystemExit("protected root must be empty before bootstrap")
for path in p["file_paths"]:
    directory(os.path.dirname(path))
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise SystemExit("managed input path already exists")
for root in p["mutable_roots"]:
    directory(root, 0o700, guest.pw_uid, guest.pw_gid)
    for current, directories, _files in os.walk(root):
        os.chown(current, guest.pw_uid, guest.pw_gid)
        os.chmod(current, 0o700)
directory(os.path.dirname(p["seal_path"]), 0o700)
""".strip()

_SEAL_SCRIPT = """
import hashlib, json, os, pwd, stat, sys
p = json.loads(sys.argv[1])
guest = pwd.getpwnam(p["guest"])
for item in p["files"]:
    info = os.lstat(item["path"])
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit("managed input is not a regular file")
    if item["read_only"]:
        with open(item["path"], "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        if actual != item["sha256"]:
            raise SystemExit("managed input digest mismatch")
        os.chown(item["path"], 0, 0)
        os.chmod(item["path"], 0o444)
    else:
        os.chown(item["path"], guest.pw_uid, guest.pw_gid)
        os.chmod(item["path"], 0o600)
for root in p["protected_roots"]:
    info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("protected root is unsafe")
    os.chown(root, 0, 0)
    os.chmod(root, 0o555)
    expected = {item["path"] for item in p["files"] if item["read_only"]}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("protected subtree is unsafe")
        os.chown(current, 0, 0)
        os.chmod(current, 0o555)
        for name in files:
            path = os.path.join(current, name)
            if path not in expected:
                raise SystemExit("protected subtree contains an unmanaged file")
seal = {
    "digest": p["digest"],
    "immutable": {item["path"]: item["sha256"] for item in p["files"] if item["read_only"]},
    "protected_roots": p["protected_roots"],
}
encoded = json.dumps(seal, sort_keys=True, separators=(",", ":")).encode()
temporary = p["seal_path"] + ".tmp"
with open(temporary, "wb") as handle:
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary, 0, 0)
os.chmod(temporary, 0o400)
os.replace(temporary, p["seal_path"])
""".strip()

_VERIFY_SCRIPT = """
import hashlib, json, os, stat, sys
p = json.loads(sys.argv[1])
info = os.lstat(p["seal_path"])
if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
    raise SystemExit("sandbox seal is unsafe")
with open(p["seal_path"], "rb") as handle:
    seal = json.loads(handle.read())
if seal.get("digest") != p["digest"]:
    raise SystemExit("sandbox seal digest mismatch")
if seal.get("immutable") != p["immutable"]:
    raise SystemExit("sandbox immutable manifest mismatch")
for path, expected in seal.get("immutable", {}).items():
    item = os.lstat(path)
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode) or item.st_uid != 0:
        raise SystemExit("sealed input metadata changed")
    if stat.S_IMODE(item.st_mode) & 0o222:
        raise SystemExit("sealed input became writable")
    with open(path, "rb") as handle:
        actual = hashlib.sha256(handle.read()).hexdigest()
    if actual != expected:
        raise SystemExit("sealed input content changed")
for root in seal.get("protected_roots", []):
    item = os.lstat(root)
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode) or item.st_uid != 0:
        raise SystemExit("protected root metadata changed")
    if stat.S_IMODE(item.st_mode) & 0o022:
        raise SystemExit("protected root became writable")
print("VMA_SEAL_OK")
""".strip()


@dataclass(frozen=True, slots=True)
class E2BDependencies:
    """Injectable async E2B bindings used by offline unit tests."""

    sandbox_class: type[Any]
    backend_class: type[Any]
    not_found_errors: tuple[type[BaseException], ...] = ()


DependencyLoader = Callable[[], E2BDependencies]


class _GuestCommands:
    def __init__(self, commands: Any, guest_user: str) -> None:
        self._commands = commands
        self._guest_user = guest_user

    async def run(self, command: str, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        result = self._commands.run(command, **kwargs)
        if not inspect.isawaitable(result):
            raise SandboxDependencyError("E2B guest command result was not awaitable")
        return await result


class _GuestFiles:
    def __init__(self, files: Any, guest_user: str) -> None:
        self._files = files
        self._guest_user = guest_user

    async def get_info(self, path: str, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        return await self._files.get_info(path, **kwargs)

    async def read(self, path: str, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        return await self._files.read(path, **kwargs)

    async def write(self, path: str, data: Any, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        return await self._files.write(path, data, **kwargs)


class _GuestSandboxView:
    """Expose only guest-bound command and filesystem handles to Deep Agents."""

    def __init__(self, native: Any, guest_user: str) -> None:
        self.native = native
        self.sandbox_id = native.sandbox_id
        self.commands = _GuestCommands(native.commands, guest_user)
        self.files = _GuestFiles(native.files, guest_user)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)


def load_e2b_dependencies() -> E2BDependencies:
    """Lazily import the official asynchronous E2B SDK and adapter."""

    try:
        e2b = importlib.import_module("e2b")
        adapter = importlib.import_module("langchain_e2b")
    except (ImportError, ModuleNotFoundError) as exc:
        raise SandboxDependencyError(
            "E2B sandbox support requires the optional 'sandbox-e2b' dependencies"
        ) from exc

    sandbox_class = getattr(e2b, "AsyncSandbox", None)
    backend_class = getattr(adapter, "AsyncE2BSandbox", None)
    not_found = getattr(e2b, "SandboxNotFoundException", None)
    if not isinstance(sandbox_class, type) or not isinstance(backend_class, type):
        raise SandboxDependencyError(
            "Installed E2B packages do not expose AsyncSandbox and AsyncE2BSandbox"
        )
    not_found_errors = (
        (not_found,)
        if isinstance(not_found, type) and issubclass(not_found, BaseException)
        else ()
    )
    return E2BDependencies(
        sandbox_class=sandbox_class,
        backend_class=backend_class,
        not_found_errors=not_found_errors,
    )


class E2BSandboxProvider:
    """Provision and manage one tenant-bound E2B sandbox per VMA session."""

    name = _PROVIDER
    capabilities = SandboxProviderCapabilities(
        execute=True,
        file_transfer=True,
        persistence=True,
        pause=True,
        network_modes=frozenset({"none", "limited", "unrestricted"}),
        secure_control_plane=True,
    )

    def __init__(
        self,
        api_key: str,
        *,
        domain: str | None = None,
        api_url: str | None = None,
        sandbox_url: str | None = None,
        default_template: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        keep_memory: bool = True,
        guest_user: str = "user",
        dependencies: DependencyLoader | E2BDependencies | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise ValueError("api_key must be a non-empty string without surrounding whitespace")
        _validate_optional_domain(domain)
        _validate_optional_url(api_url, "api_url")
        _validate_optional_url(sandbox_url, "sandbox_url")
        _validate_optional_name(default_template, "default_template")
        if keep_memory is not True:
            raise ValueError("E2B session persistence requires keep_memory=True")
        if (
            not isinstance(guest_user, str)
            or not _GUEST_USER.fullmatch(guest_user)
            or guest_user == "root"
        ):
            raise ValueError("guest_user must be a safe non-root Linux account name")

        defaults = SandboxPolicy(
            timeout_seconds=timeout,
            command_timeout_seconds=command_timeout,
        ).resolved(
            default_timeout_seconds=timeout,
            default_command_timeout_seconds=command_timeout,
        )
        self._api_key = api_key
        self._domain = domain
        self._api_url = api_url
        self._sandbox_url = sandbox_url
        self._default_template = default_template
        self._timeout = defaults.timeout_seconds
        self._command_timeout = defaults.command_timeout_seconds
        self._guest_user = guest_user
        if isinstance(dependencies, E2BDependencies):
            self._load_dependencies: DependencyLoader = lambda: dependencies
        else:
            self._load_dependencies = dependencies or load_e2b_dependencies

    async def provision(
        self,
        owner: SandboxOwner,
        policy: SandboxPolicy,
        *,
        template: str | None = None,
    ) -> SandboxConnection:
        """Create one isolated sandbox and return its Deep Agents backend."""

        resolved = self._resolve_policy(policy)
        requested_template = template if template is not None else self._default_template
        _validate_optional_name(requested_template, "template")
        dependencies = self._dependencies()
        create_kwargs: dict[str, Any] = {
            **self._api_options(),
            "timeout": resolved.timeout_seconds,
            "metadata": self._ownership_metadata(owner, resolved.fingerprint),
            "secure": True,
            "lifecycle": self._lifecycle_options(resolved),
            **self._network_options(resolved),
        }
        if requested_template is not None:
            create_kwargs["template"] = requested_template

        native = await self._sdk_call(
            "provision",
            dependencies,
            lambda: dependencies.sandbox_class.create(**create_kwargs),
        )
        try:
            external_id = self._native_id(native)
            info = await self._native_info(native, dependencies, operation="provision")
            self._verify_info(
                info,
                external_id=external_id,
                owner=owner,
                policy_fingerprint=resolved.fingerprint,
            )
            self._verify_remote_policy(info, resolved)
            provider_template = _info_value(info, "template_id", "templateID")
            reference = SandboxReference(
                provider=self.name,
                external_id=external_id,
                owner_fingerprint=owner.fingerprint,
                policy_fingerprint=resolved.fingerprint,
                template_id=str(provider_template) if provider_template else requested_template,
            )
            return self._connection(native, reference, resolved, dependencies, info)
        except BaseException:
            await self._best_effort_kill(native)
            raise

    async def connect(
        self,
        reference: SandboxReference,
        owner: SandboxOwner,
        policy: SandboxPolicy,
    ) -> SandboxConnection:
        """Reconnect to the exact persisted sandbox, resuming it if paused."""

        resolved = self._resolve_policy(policy)
        reference.assert_access(provider=self.name, owner=owner, policy=resolved)
        dependencies = self._dependencies()

        info = await self._static_info(reference, owner, dependencies, operation="connect")
        self._verify_remote_policy(info, resolved)
        native = await self._sdk_call(
            "connect",
            dependencies,
            lambda: dependencies.sandbox_class.connect(
                reference.external_id,
                timeout=resolved.timeout_seconds,
                **self._api_options(),
            ),
        )
        try:
            if self._native_id(native) != reference.external_id:
                raise SandboxOwnershipError("E2B returned a different opaque sandbox identifier")
            connected_info = await self._native_info(native, dependencies, operation="connect")
            self._verify_info(
                connected_info,
                external_id=reference.external_id,
                owner=owner,
                policy_fingerprint=reference.policy_fingerprint,
                template_id=reference.template_id,
            )
            self._verify_remote_policy(connected_info, resolved)
            return self._connection(native, reference, resolved, dependencies, connected_info)
        except BaseException:
            await self._best_effort_pause(native)
            raise

    async def pause(self, reference: SandboxReference, owner: SandboxOwner) -> None:
        """Pause a session sandbox without resuming it and preserve memory."""

        reference.assert_access(provider=self.name, owner=owner)
        dependencies = self._dependencies()
        await self._static_info(reference, owner, dependencies, operation="pause")
        result = await self._sdk_call(
            "pause",
            dependencies,
            lambda: dependencies.sandbox_class.pause(
                reference.external_id,
                keep_memory=True,
                **self._api_options(),
            ),
        )
        if not isinstance(result, bool):
            raise SandboxOperationError("E2B pause returned an invalid result")

    async def delete(self, reference: SandboxReference, owner: SandboxOwner) -> None:
        """Permanently delete a session sandbox without first resuming it."""

        reference.assert_access(provider=self.name, owner=owner)
        dependencies = self._dependencies()
        await self._static_info(reference, owner, dependencies, operation="delete")
        deleted = await self._sdk_call(
            "delete",
            dependencies,
            lambda: dependencies.sandbox_class.kill(
                reference.external_id,
                **self._api_options(),
            ),
        )
        if deleted is not True:
            raise SandboxNotFoundError("E2B sandbox was not found during deletion")

    async def bootstrap(
        self,
        connection: SandboxConnection,
        *,
        files: list[tuple[str, bytes]],
        read_only_paths: tuple[str, ...],
        mutable_roots: tuple[str, ...],
        digest: str,
    ) -> None:
        """Upload initial inputs once as root and seal immutable content."""
        native = connection.native
        if native is None:
            raise SandboxDependencyError("E2B connection does not expose its native sandbox")
        _validate_digest(digest)
        normalized_files = _normalized_files(files)
        read_only = set(read_only_paths)
        if not read_only.issubset(normalized_files):
            raise SandboxPolicyError("read_only_paths must refer to uploaded files")
        normalized_roots = tuple(sorted({_normalized_path(path, directory=True) for path in mutable_roots}))
        protected_roots = _protected_roots(read_only, normalized_roots)

        await self._attest_guest(native, operation="bootstrap guest attestation")
        await self._run_root(
            native,
            _PREPARE_SCRIPT,
            {
                "guest": self._guest_user,
                "mutable_roots": list(normalized_roots),
                "protected_roots": list(protected_roots),
                "file_paths": sorted(normalized_files),
                "seal_path": _SEAL_PATH,
            },
            operation="bootstrap prepare",
        )
        try:
            for path, content in normalized_files.items():
                result = native.files.write(path, content, user="root")
                if not inspect.isawaitable(result):
                    raise SandboxDependencyError("E2B async filesystem write was not awaitable")
                await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxOperationError("E2B bootstrap upload failed") from exc

        entries = [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "read_only": path in read_only,
            }
            for path, content in sorted(normalized_files.items())
        ]
        await self._run_root(
            native,
            _SEAL_SCRIPT,
            {
                "guest": self._guest_user,
                "files": entries,
                "protected_roots": list(protected_roots),
                "digest": digest,
                "seal_path": _SEAL_PATH,
            },
            operation="bootstrap seal",
        )

    async def verify_bootstrap(
        self,
        connection: SandboxConnection,
        *,
        digest: str,
        immutable_manifest: dict[str, str],
    ) -> None:
        """Fail closed before a turn if immutable sandbox inputs drifted."""
        native = connection.native
        if native is None:
            raise SandboxDependencyError("E2B connection does not expose its native sandbox")
        _validate_digest(digest)
        manifest = _normalized_manifest(immutable_manifest)
        await self._attest_guest(native, operation="resume guest attestation")
        await self._run_root(
            native,
            _VERIFY_SCRIPT,
            {
                "digest": digest,
                "immutable": manifest,
                "seal_path": _SEAL_PATH,
            },
            operation="bootstrap verification",
        )

    async def _attest_guest(self, native: Any, *, operation: str) -> None:
        commands = getattr(native, "commands", None)
        run = getattr(commands, "run", None)
        if not callable(run):
            raise SandboxDependencyError("E2B sandbox does not expose async commands.run")
        try:
            result = run(
                _GUEST_ATTEST_COMMAND,
                envs={"VMA_EXPECTED_GUEST": self._guest_user},
                timeout=self._command_timeout,
            )
            if not inspect.isawaitable(result):
                raise SandboxDependencyError("E2B guest attestation was not awaitable")
            completed = await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxPolicyError(f"E2B {operation} failed") from exc
        if getattr(completed, "exit_code", None) != 0:
            raise SandboxPolicyError(
                "E2B template guest identity or privilege boundary is unsafe"
            )

    async def _run_root(
        self,
        native: Any,
        script: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> None:
        commands = getattr(native, "commands", None)
        run = getattr(commands, "run", None)
        if not callable(run):
            raise SandboxDependencyError("E2B sandbox does not expose async commands.run")
        command = "python3 -c " + shlex.quote(script) + " " + shlex.quote(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
        try:
            result = run(
                command,
                user="root",
                timeout=self._command_timeout,
            )
            if not inspect.isawaitable(result):
                raise SandboxDependencyError("E2B async command result was not awaitable")
            completed = await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxOperationError(f"E2B {operation} failed") from exc
        exit_code = getattr(completed, "exit_code", None)
        if exit_code != 0:
            raise SandboxOperationError(f"E2B {operation} failed")

    def _resolve_policy(self, policy: SandboxPolicy) -> ResolvedSandboxPolicy:
        if not isinstance(policy, SandboxPolicy):
            raise SandboxPolicyError("policy must be a SandboxPolicy")
        resolved = policy.resolved(
            default_timeout_seconds=self._timeout,
            default_command_timeout_seconds=self._command_timeout,
        )
        self.capabilities.validate(resolved)
        return resolved

    def _dependencies(self) -> E2BDependencies:
        try:
            dependencies = self._load_dependencies()
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxDependencyError("Unable to load E2B sandbox dependencies") from exc
        if not isinstance(dependencies, E2BDependencies):
            raise SandboxDependencyError("E2B dependency loader returned invalid bindings")
        for method in ("create", "connect", "get_info", "pause", "kill"):
            if not callable(getattr(dependencies.sandbox_class, method, None)):
                raise SandboxDependencyError(f"E2B async SDK is missing Sandbox.{method}")
        if not isinstance(dependencies.backend_class, type):
            raise SandboxDependencyError("langchain-e2b adapter binding is invalid")
        return dependencies

    def _connection(
        self,
        native: Any,
        reference: SandboxReference,
        policy: ResolvedSandboxPolicy,
        dependencies: E2BDependencies,
        info: Any,
    ) -> SandboxConnection:
        try:
            backend = dependencies.backend_class(
                sandbox=_GuestSandboxView(native, self._guest_user),
                workdir=policy.workdir,
                timeout=policy.command_timeout_seconds,
            )
        except Exception as exc:
            raise SandboxDependencyError(
                "Official langchain-e2b adapter could not wrap the sandbox"
            ) from exc

        from deepagents.backends.protocol import SandboxBackendProtocol

        if not isinstance(backend, SandboxBackendProtocol):
            raise SandboxDependencyError(
                "Official langchain-e2b adapter does not implement SandboxBackendProtocol"
            )
        config: dict[str, Any] = {
            "policy": policy.to_dict(),
            "keep_memory": True,
            "configured_template": self._default_template,
            "domain": self._domain,
            "api_url": self._api_url,
            "sandbox_url": self._sandbox_url,
            "guest_user": self._guest_user,
            **reference.to_config(),
        }
        if reference.template_id is not None:
            config["template_id"] = reference.template_id
        return SandboxConnection(
            reference=reference,
            backend=cast("SandboxBackendProtocol", backend),
            native=native,
            config=config,
            capabilities=self.capabilities.to_dict(),
            metadata={
                "provider": self.name,
                "state": _state_value(_info_value(info, "state")),
            },
        )

    async def _static_info(
        self,
        reference: SandboxReference,
        owner: SandboxOwner,
        dependencies: E2BDependencies,
        *,
        operation: str,
    ) -> Any:
        info = await self._sdk_call(
            f"{operation} ownership verification",
            dependencies,
            lambda: dependencies.sandbox_class.get_info(
                reference.external_id,
                **self._api_options(),
            ),
        )
        self._verify_info(
            info,
            external_id=reference.external_id,
            owner=owner,
            policy_fingerprint=reference.policy_fingerprint,
            template_id=reference.template_id,
        )
        return info

    async def _native_info(
        self,
        native: Any,
        dependencies: E2BDependencies,
        *,
        operation: str,
    ) -> Any:
        get_info = getattr(native, "get_info", None)
        if not callable(get_info):
            raise SandboxDependencyError("E2B sandbox does not expose get_info")
        return await self._sdk_call(
            f"{operation} ownership verification",
            dependencies,
            lambda: get_info(**self._api_options()),
        )

    async def _sdk_call(
        self,
        operation: str,
        dependencies: E2BDependencies,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            result = call()
            if not inspect.isawaitable(result):
                raise SandboxDependencyError(
                    "E2B async SDK returned a non-awaitable lifecycle result"
                )
            return await result
        except (SandboxDependencyError, SandboxNotFoundError):
            raise
        except Exception as exc:
            if dependencies.not_found_errors and isinstance(exc, dependencies.not_found_errors):
                raise SandboxNotFoundError("E2B sandbox was not found") from exc
            raise SandboxOperationError(f"E2B {operation} failed") from exc

    async def _best_effort_kill(self, native: Any) -> None:
        kill = getattr(native, "kill", None)
        if not callable(kill):
            return
        try:
            result = kill(**self._api_options())
            if inspect.isawaitable(result):
                async with asyncio.timeout(10):
                    await result
        except BaseException:
            return

    async def _best_effort_pause(self, native: Any) -> None:
        pause = getattr(native, "pause", None)
        if not callable(pause):
            return
        try:
            result = pause(keep_memory=True, **self._api_options())
            if inspect.isawaitable(result):
                async with asyncio.timeout(10):
                    await asyncio.shield(result)
        except BaseException:
            return

    def _api_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"api_key": self._api_key}
        for name, value in (
            ("domain", self._domain),
            ("api_url", self._api_url),
            ("sandbox_url", self._sandbox_url),
        ):
            if value is not None:
                options[name] = value
        return options

    @staticmethod
    def _network_options(policy: ResolvedSandboxPolicy) -> dict[str, Any]:
        network: dict[str, Any] = {"allow_public_traffic": False}
        if policy.network_access == "unrestricted":
            return {"allow_internet_access": True, "network": network}
        if policy.network_access == "limited":
            network["allow_out"] = list(policy.allowed_egress)
            return {"allow_internet_access": True, "network": network}
        return {"allow_internet_access": False, "network": network}

    @staticmethod
    def _lifecycle_options(policy: ResolvedSandboxPolicy) -> dict[str, Any]:
        on_timeout: str | dict[str, Any]
        if policy.auto_pause:
            on_timeout = {"action": "pause", "keep_memory": True}
        else:
            on_timeout = "kill"
        return {"on_timeout": on_timeout, "auto_resume": False}

    @staticmethod
    def _ownership_metadata(owner: SandboxOwner, policy_fingerprint: str) -> dict[str, str]:
        return {
            _MANAGED_BY_METADATA_KEY: _MANAGED_BY,
            _OWNER_METADATA_KEY: owner.fingerprint,
            _POLICY_METADATA_KEY: policy_fingerprint,
        }

    def _verify_info(
        self,
        info: Any,
        *,
        external_id: str,
        owner: SandboxOwner,
        policy_fingerprint: str,
        template_id: str | None = None,
    ) -> None:
        remote_id = _info_value(info, "sandbox_id", "sandboxID")
        if remote_id != external_id:
            raise SandboxOwnershipError("E2B returned a different opaque sandbox identifier")
        metadata = _info_value(info, "metadata")
        if not isinstance(metadata, dict):
            raise SandboxOwnershipError("E2B sandbox ownership metadata is unavailable")
        expected = self._ownership_metadata(owner, policy_fingerprint)
        if metadata.get(_MANAGED_BY_METADATA_KEY) != expected[_MANAGED_BY_METADATA_KEY]:
            raise SandboxOwnershipError("E2B sandbox is not managed by this control plane")
        if metadata.get(_OWNER_METADATA_KEY) != expected[_OWNER_METADATA_KEY]:
            raise SandboxOwnershipError("E2B sandbox belongs to a different tenant scope")
        if metadata.get(_POLICY_METADATA_KEY) != expected[_POLICY_METADATA_KEY]:
            raise SandboxPolicyError("E2B sandbox policy metadata does not match the reference")
        remote_template = _info_value(info, "template_id", "templateID")
        if template_id is not None and remote_template != template_id:
            raise SandboxPolicyError("E2B sandbox template no longer matches the reference")

    @staticmethod
    def _verify_remote_policy(info: Any, policy: ResolvedSandboxPolicy) -> None:
        expected_internet = policy.network_access != "none"
        actual_internet = _info_value(info, "allow_internet_access", "allowInternetAccess")
        if not isinstance(actual_internet, bool) or actual_internet is not expected_internet:
            raise SandboxPolicyError("E2B did not enforce the requested internet-access policy")

        network = _info_value(info, "network")
        if not isinstance(network, dict) or network.get("allow_public_traffic") is not False:
            raise SandboxPolicyError("E2B public sandbox traffic is not verifiably disabled")
        if policy.network_access == "limited" and sorted(network.get("allow_out") or []) != sorted(
            policy.allowed_egress
        ):
            raise SandboxPolicyError("E2B outbound allowlist does not match the requested policy")

        lifecycle = _info_value(info, "lifecycle")
        expected_on_timeout = "pause" if policy.auto_pause else "kill"
        if (
            not isinstance(lifecycle, dict)
            or lifecycle.get("on_timeout") != expected_on_timeout
            or lifecycle.get("auto_resume") is not False
        ):
            raise SandboxPolicyError("E2B lifecycle policy does not match the requested policy")

    @staticmethod
    def _native_id(native: Any) -> str:
        external_id = getattr(native, "sandbox_id", None)
        if not isinstance(external_id, str) or not external_id or external_id != external_id.strip():
            raise SandboxOperationError("E2B returned an invalid opaque sandbox identifier")
        return external_id


def _info_value(info: Any, *names: str) -> Any:
    for name in names:
        if isinstance(info, dict) and name in info:
            return info[name]
        value = getattr(info, name, None)
        if value is not None:
            return value
    return None


def _state_value(state: Any) -> str:
    value = getattr(state, "value", state)
    return str(value or "running")


def _validate_optional_name(value: str | None, name: str) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")


def _validate_optional_domain(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 253
        or any(char in value for char in ("/", ":", "@", "?", "#"))
    ):
        raise ValueError("domain must be a hostname without a scheme or path")


def _validate_optional_url(value: str | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value != value.strip() or len(value) > 2048:
        raise ValueError(f"{name} must be a bounded URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTP(S) URL without credentials, query, or fragment")


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise SandboxPolicyError("bootstrap digest must be a SHA-256 identifier")


def _normalized_path(value: str, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise SandboxPolicyError("managed sandbox paths must be absolute")
    path = PurePosixPath(value)
    normalized = str(path)
    if value != normalized or value == "/" or ".." in path.parts:
        raise SandboxPolicyError("managed sandbox paths must be normalized below root")
    if directory and normalized in {"/skills", "/mnt", "/var", "/home"}:
        raise SandboxPolicyError("mutable roots must not claim a shared system directory")
    return normalized


def _normalized_files(files: list[tuple[str, bytes]]) -> dict[str, bytes]:
    if not isinstance(files, list):
        raise SandboxPolicyError("bootstrap files must be a list")
    result: dict[str, bytes] = {}
    for raw_path, content in files:
        path = _normalized_path(raw_path)
        if not isinstance(content, bytes):
            raise SandboxPolicyError("bootstrap file content must be bytes")
        existing = result.get(path)
        if existing is not None and existing != content:
            raise SandboxPolicyError(f"conflicting bootstrap file content at {path}")
        for other in result:
            if path.startswith(other + "/") or other.startswith(path + "/"):
                raise SandboxPolicyError("bootstrap path is both a file and directory")
        result[path] = content
    return result


def _normalized_manifest(manifest: dict[str, str]) -> dict[str, str]:
    if not isinstance(manifest, dict):
        raise SandboxPolicyError("immutable manifest must be an object")
    result: dict[str, str] = {}
    for raw_path, digest in manifest.items():
        path = _normalized_path(raw_path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SandboxPolicyError("immutable manifest contains an invalid digest")
        result[path] = digest
    return dict(sorted(result.items()))


def _protected_roots(
    read_only_paths: set[str],
    mutable_roots: tuple[str, ...],
) -> tuple[str, ...]:
    roots: set[str] = set()
    for path in read_only_paths:
        parts = PurePosixPath(path).parts
        if path.startswith("/skills/custom/"):
            root = "/skills/custom"
        elif path.startswith("/mnt/session/"):
            root = "/mnt/session"
        elif path.startswith("/mnt/memory/") and len(parts) >= 4:
            root = "/" + "/".join(parts[1:4])
        else:
            root = str(PurePosixPath(path).parent)
        if any(
            root == mutable or root.startswith(mutable.rstrip("/") + "/")
            or mutable.startswith(root.rstrip("/") + "/")
            for mutable in mutable_roots
        ):
            raise SandboxPolicyError("read-only inputs overlap a mutable sandbox root")
        roots.add(root)
    return tuple(sorted(roots))


__all__ = ["E2BDependencies", "E2BSandboxProvider", "load_e2b_dependencies"]

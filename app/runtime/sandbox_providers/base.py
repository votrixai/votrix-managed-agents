"""Provider-neutral sandbox lifecycle contracts.

The control plane owns tenant scoping and persistence. Provider identifiers are
opaque and must never be used as an authorization boundary by themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from deepagents.backends.protocol import SandboxBackendProtocol


NetworkAccess = Literal["none", "limited", "unrestricted"]
_NETWORK_ACCESS = frozenset({"none", "limited", "unrestricted"})
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_MAX_OWNER_ID_BYTES = 128
_MAX_PROVIDER_BYTES = 64
_MAX_EXTERNAL_ID_BYTES = 512
_MAX_TIMEOUT_SECONDS = 24 * 60 * 60


class SandboxProviderError(RuntimeError):
    """Base error for provider lifecycle failures."""


class SandboxDependencyError(SandboxProviderError):
    """Raised when an optional provider SDK or adapter is unavailable."""


class SandboxPolicyError(SandboxProviderError, ValueError):
    """Raised when a requested policy cannot be enforced."""


class SandboxOwnershipError(SandboxProviderError):
    """Raised when a sandbox reference does not belong to the caller."""


class SandboxNotFoundError(SandboxProviderError):
    """Raised when the provider no longer has the referenced sandbox."""


class SandboxOperationError(SandboxProviderError):
    """Raised when a provider lifecycle operation fails safely."""


@dataclass(frozen=True, slots=True)
class SandboxOwner:
    """Trusted ownership scope for one session sandbox."""

    workspace_id: str
    session_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.workspace_id, "workspace_id", max_bytes=_MAX_OWNER_ID_BYTES)
        _validate_identifier(self.session_id, "session_id", max_bytes=_MAX_OWNER_ID_BYTES)

    @property
    def fingerprint(self) -> str:
        """Return a stable pseudonymous key safe for provider metadata."""

        return _fingerprint(
            "owner-v1",
            {"workspace_id": self.workspace_id, "session_id": self.session_id},
        )


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Security and lifecycle requirements for a session sandbox."""

    network_access: NetworkAccess = "none"
    allowed_egress: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    command_timeout_seconds: int | None = None
    workdir: str = "/home/user"
    auto_pause: bool = True
    require_execute: bool = True
    require_file_transfer: bool = True
    require_persistence: bool = True
    require_pause: bool = True

    def __post_init__(self) -> None:
        if self.network_access not in _NETWORK_ACCESS:
            raise SandboxPolicyError(f"Unsupported network access mode: {self.network_access!r}")

        raw_egress = self.allowed_egress
        if isinstance(raw_egress, str):
            raise SandboxPolicyError("allowed_egress must be a sequence of destinations")
        try:
            normalized_egress = tuple(sorted(set(raw_egress)))
        except TypeError as exc:
            raise SandboxPolicyError("allowed_egress must contain strings") from exc
        if any(not isinstance(value, str) for value in normalized_egress):
            raise SandboxPolicyError("allowed_egress must contain strings")
        for destination in normalized_egress:
            _validate_egress_destination(destination)
        object.__setattr__(self, "allowed_egress", normalized_egress)

        if self.network_access == "limited" and not normalized_egress:
            raise SandboxPolicyError("limited network access requires allowed_egress")
        if self.network_access != "limited" and normalized_egress:
            raise SandboxPolicyError("allowed_egress is only valid with limited network access")

        _validate_optional_timeout(self.timeout_seconds, "timeout_seconds")
        _validate_optional_timeout(self.command_timeout_seconds, "command_timeout_seconds")
        _validate_workdir(self.workdir)
        for name in (
            "auto_pause",
            "require_execute",
            "require_file_transfer",
            "require_persistence",
            "require_pause",
        ):
            if not isinstance(getattr(self, name), bool):
                raise SandboxPolicyError(f"{name} must be a boolean")

    def resolved(
        self,
        *,
        default_timeout_seconds: int,
        default_command_timeout_seconds: int,
    ) -> ResolvedSandboxPolicy:
        timeout = self.timeout_seconds or default_timeout_seconds
        command_timeout = self.command_timeout_seconds or default_command_timeout_seconds
        _validate_timeout(timeout, "timeout_seconds")
        _validate_timeout(command_timeout, "command_timeout_seconds")
        if command_timeout > timeout:
            raise SandboxPolicyError("command_timeout_seconds cannot exceed timeout_seconds")
        return ResolvedSandboxPolicy(
            network_access=self.network_access,
            allowed_egress=self.allowed_egress,
            timeout_seconds=timeout,
            command_timeout_seconds=command_timeout,
            workdir=self.workdir,
            auto_pause=self.auto_pause,
            require_execute=self.require_execute,
            require_file_transfer=self.require_file_transfer,
            require_persistence=self.require_persistence,
            require_pause=self.require_pause,
        )


@dataclass(frozen=True, slots=True)
class ResolvedSandboxPolicy:
    """A fully resolved policy persisted with a provider reference."""

    network_access: NetworkAccess
    allowed_egress: tuple[str, ...]
    timeout_seconds: int
    command_timeout_seconds: int
    workdir: str
    auto_pause: bool
    require_execute: bool
    require_file_transfer: bool
    require_persistence: bool
    require_pause: bool

    @property
    def fingerprint(self) -> str:
        return _fingerprint("policy-v1", self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_access": self.network_access,
            "allowed_egress": list(self.allowed_egress),
            "timeout_seconds": self.timeout_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "workdir": self.workdir,
            "auto_pause": self.auto_pause,
            "require_execute": self.require_execute,
            "require_file_transfer": self.require_file_transfer,
            "require_persistence": self.require_persistence,
            "require_pause": self.require_pause,
        }


@dataclass(frozen=True, slots=True)
class SandboxProviderCapabilities:
    """Capabilities a provider can enforce."""

    execute: bool
    file_transfer: bool
    persistence: bool
    pause: bool
    network_modes: frozenset[NetworkAccess]
    secure_control_plane: bool

    def validate(self, policy: ResolvedSandboxPolicy) -> None:
        missing: list[str] = []
        if policy.require_execute and not self.execute:
            missing.append("execute")
        if policy.require_file_transfer and not self.file_transfer:
            missing.append("file_transfer")
        if policy.require_persistence and not self.persistence:
            missing.append("persistence")
        if (policy.require_pause or policy.auto_pause) and not self.pause:
            missing.append("pause")
        if policy.network_access not in self.network_modes:
            missing.append(f"network:{policy.network_access}")
        if not self.secure_control_plane:
            missing.append("secure_control_plane")
        if missing:
            raise SandboxPolicyError(
                "Sandbox provider cannot enforce required capabilities: " + ", ".join(missing)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execute": self.execute,
            "file_transfer": self.file_transfer,
            "persistence": self.persistence,
            "pause": self.pause,
            "network_modes": sorted(self.network_modes),
            "secure_control_plane": self.secure_control_plane,
        }


@dataclass(frozen=True, slots=True)
class SandboxReference:
    """Opaque provider reference plus trusted ownership/policy bindings."""

    provider: str
    external_id: str
    owner_fingerprint: str
    policy_fingerprint: str
    template_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.provider, "provider", max_bytes=_MAX_PROVIDER_BYTES)
        _validate_identifier(self.external_id, "external_id", max_bytes=_MAX_EXTERNAL_ID_BYTES)
        for name in ("owner_fingerprint", "policy_fingerprint"):
            if not _HEX_SHA256.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        if self.template_id is not None:
            _validate_identifier(self.template_id, "template_id", max_bytes=_MAX_EXTERNAL_ID_BYTES)

    def assert_access(
        self,
        *,
        provider: str,
        owner: SandboxOwner,
        policy: ResolvedSandboxPolicy | None = None,
    ) -> None:
        if self.provider != provider:
            raise SandboxOwnershipError("Sandbox reference belongs to a different provider")
        if not hmac.compare_digest(self.owner_fingerprint, owner.fingerprint):
            raise SandboxOwnershipError("Sandbox reference belongs to a different tenant scope")
        if policy is not None and not hmac.compare_digest(self.policy_fingerprint, policy.fingerprint):
            raise SandboxPolicyError("Sandbox policy does not match the provisioned policy")

    def to_config(self) -> dict[str, str]:
        """Return non-secret binding data suitable for persistence."""

        return {
            "owner_fingerprint": self.owner_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SandboxConnection:
    """A connected provider sandbox and its Deep Agents backend."""

    reference: SandboxReference
    backend: SandboxBackendProtocol = field(repr=False)
    native: Any = field(default=None, repr=False, compare=False)
    config: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "capabilities", dict(self.capabilities))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def external_id(self) -> str:
        return self.reference.external_id

    def to_record(self) -> dict[str, Any]:
        """Return fields accepted by the session-sandbox persistence layer."""

        return {
            "provider": self.reference.provider,
            "external_sandbox_id": self.reference.external_id,
            "template_id": self.reference.template_id,
            "state": str(self.metadata.get("state") or "running"),
            "config": dict(self.config),
            "capabilities": dict(self.capabilities),
        }


class SandboxLifecycleProvider(Protocol):
    """Async provider interface consumed by the managed control plane."""

    name: str
    capabilities: SandboxProviderCapabilities

    async def provision(
        self,
        owner: SandboxOwner,
        policy: SandboxPolicy,
        *,
        template: str | None = None,
    ) -> SandboxConnection: ...

    async def connect(
        self,
        reference: SandboxReference,
        owner: SandboxOwner,
        policy: SandboxPolicy,
    ) -> SandboxConnection: ...

    async def pause(self, reference: SandboxReference, owner: SandboxOwner) -> None: ...

    async def delete(self, reference: SandboxReference, owner: SandboxOwner) -> None: ...


def _fingerprint(namespace: str, value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(namespace.encode() + b"\0" + encoded).hexdigest()


def _validate_identifier(value: str, name: str, *, max_bytes: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} is too long")


def _validate_optional_timeout(value: int | None, name: str) -> None:
    if value is not None:
        _validate_timeout(value, name)


def _validate_timeout(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise SandboxPolicyError(f"{name} must be an integer between 1 and {_MAX_TIMEOUT_SECONDS}")


def _validate_workdir(value: str) -> None:
    if not isinstance(value, str) or not value.startswith("/") or value != value.strip():
        raise SandboxPolicyError("workdir must be a normalized absolute POSIX path")
    path = PurePosixPath(value)
    if value == "/" or ".." in path.parts or str(path) != value:
        raise SandboxPolicyError("workdir must be a normalized absolute POSIX path below root")


def _validate_egress_destination(value: str) -> None:
    if not value or value != value.strip() or len(value) > 253:
        raise SandboxPolicyError(f"Invalid egress destination: {value!r}")
    try:
        ipaddress.ip_network(value, strict=False)
        return
    except ValueError:
        pass

    candidate = value.lower()
    if candidate.startswith("*."):
        candidate = candidate[2:]
    if any(char in candidate for char in ("/", ":", "@", "?", "#")):
        raise SandboxPolicyError(f"Invalid egress destination: {value!r}")
    labels = candidate.rstrip(".").split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise SandboxPolicyError(f"Invalid egress destination: {value!r}")


__all__ = [
    "NetworkAccess",
    "ResolvedSandboxPolicy",
    "SandboxConnection",
    "SandboxDependencyError",
    "SandboxLifecycleProvider",
    "SandboxNotFoundError",
    "SandboxOperationError",
    "SandboxOwner",
    "SandboxOwnershipError",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxProviderCapabilities",
    "SandboxProviderError",
    "SandboxReference",
]

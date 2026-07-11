"""One persistent E2B sandbox for one managed session.

An E2B-backed session is provisioned and seeded exactly once. Skills, initial
inputs, and memory seeds are bound to a fixed identity; read-only files are
also sealed in the sandbox. Every later turn reconnects the same opaque
provider ID and refuses to run if the create-time identity changed.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import environments as environments_q
from app.db.queries import session_sandboxes as sandboxes_q
from app.db.queries import sessions as sessions_q
from app.runtime.sandbox_inputs import (
    SandboxInputBundle,
    SandboxInputError,
    sandbox_input_bundle,
)
from app.runtime.sandbox_providers import (
    E2BSandboxProvider,
    SandboxConnection,
    SandboxNotFoundError,
    SandboxOwner,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxProviderError,
    SandboxReference,
)

logger = structlog.get_logger()
E2B_PROVIDER = "e2b"
STATE_PROVIDER = "state"
SUPPORTED_PROVIDERS = frozenset({E2B_PROVIDER, STATE_PROVIDER})


class SandboxLifecycleError(RuntimeError):
    """Base control-plane lifecycle error."""


class SandboxLifecycleConfigurationError(SandboxLifecycleError):
    """The requested environment cannot be enforced by this deployment."""


class SandboxInputMismatchError(SandboxLifecycleError):
    """A caller tried to resume with different sealed session inputs."""


def selected_sandbox_provider(environment_config: dict[str, Any] | None) -> str:
    settings = get_settings()
    configured = str(settings.vma_sandbox_provider or STATE_PROVIDER).strip().lower()
    if configured not in SUPPORTED_PROVIDERS:
        raise SandboxLifecycleConfigurationError(
            f"Unsupported VMA_SANDBOX_PROVIDER: {configured!r}"
        )

    config = dict(environment_config or {})
    environment_type = str(config.get("type") or "cloud")
    requested = str((config.get("sandbox") or {}).get("backend") or "").strip().lower()
    if requested == E2B_PROVIDER and configured != E2B_PROVIDER:
        raise SandboxLifecycleConfigurationError(
            "This environment requests E2B, but VMA_SANDBOX_PROVIDER is not e2b"
        )
    if environment_type != "cloud":
        return STATE_PROVIDER
    if requested in {"unix_local", "self_hosted_worker"}:
        return STATE_PROVIDER
    if settings.vma_sandbox_factory and configured == E2B_PROVIDER:
        raise SandboxLifecycleConfigurationError(
            "VMA_SANDBOX_FACTORY cannot be combined with VMA_SANDBOX_PROVIDER=e2b"
        )
    return configured


def sandbox_policy_from_environment(
    environment_config: dict[str, Any] | None,
) -> SandboxPolicy:
    settings = get_settings()
    config = dict(environment_config or {})
    sandbox = dict(config.get("sandbox") or {})
    networking = dict(config.get("networking") or {"type": "unrestricted"})
    network_access = str(networking.get("type") or "unrestricted").lower()
    if network_access == "restricted":
        network_access = "limited"
    if network_access not in {"none", "limited", "unrestricted"}:
        raise SandboxLifecycleConfigurationError(
            f"Unsupported E2B network mode: {network_access!r}"
        )
    allowed_egress = tuple(str(item) for item in networking.get("allowed_hosts") or [])
    if network_access == "limited" and not allowed_egress:
        network_access = "none"

    packages = dict(config.get("packages") or {})
    if any(packages.get(manager) for manager in ("apt", "cargo", "gem", "go", "npm", "pip")):
        raise SandboxLifecycleConfigurationError(
            "E2B session packages must be built into VMA_E2B_TEMPLATE"
        )

    resources = dict(config.get("resources") or {})
    declared_profile = dict(settings.vma_e2b_template_resources or {})
    for name in ("cpu", "memory_mb", "disk_mb"):
        requested = resources.get(name)
        if requested is not None and requested != declared_profile.get(name):
            raise SandboxLifecycleConfigurationError(
                f"Requested {name} does not match VMA_E2B_TEMPLATE_RESOURCES"
            )

    if bool(sandbox.get("auto_resume", settings.vma_e2b_auto_resume)):
        raise SandboxLifecycleConfigurationError(
            "E2B auto-resume must stay disabled; VMA explicitly reconnects each turn"
        )
    if not bool(sandbox.get("keep_memory", settings.vma_e2b_keep_memory)):
        raise SandboxLifecycleConfigurationError(
            "E2B session persistence requires full-memory pause"
        )
    if bool(sandbox.get("allow_public_traffic", settings.vma_e2b_allow_public_traffic)):
        raise SandboxLifecycleConfigurationError("Public E2B sandbox traffic is disabled")
    if not bool(sandbox.get("auto_pause", settings.vma_e2b_auto_pause)):
        raise SandboxLifecycleConfigurationError(
            "E2B timeout handling must pause and preserve the Session sandbox"
        )
    if not bool(sandbox.get("pause_on_exit", settings.vma_e2b_pause_on_exit)):
        raise SandboxLifecycleConfigurationError(
            "E2B turn exit must pause and preserve the Session sandbox"
        )

    configured_template = _optional(settings.vma_e2b_template)
    if configured_template is None:
        raise SandboxLifecycleConfigurationError(
            "VMA_E2B_TEMPLATE must name an operator-owned hardened template"
        )
    requested_template = _optional(sandbox.get("template"))
    if requested_template is not None and requested_template != configured_template:
        raise SandboxLifecycleConfigurationError(
            "sandbox.template must match the operator-controlled VMA_E2B_TEMPLATE"
        )

    timeout = resources.get("timeout_seconds") or sandbox.get("idle_timeout_seconds")
    timeout = int(timeout or settings.vma_e2b_timeout_seconds)
    command_timeout = min(int(settings.vma_sandbox_command_timeout_seconds), timeout)
    workdir = str(settings.vma_e2b_workdir or "/workspace")
    requested_workdir = _optional(sandbox.get("workdir") or sandbox.get("root"))
    if requested_workdir is not None and requested_workdir != workdir:
        raise SandboxLifecycleConfigurationError(
            "sandbox.workdir/root must match operator-controlled VMA_E2B_WORKDIR"
        )
    try:
        return SandboxPolicy(
            network_access=network_access,  # type: ignore[arg-type]
            allowed_egress=allowed_egress,
            timeout_seconds=timeout,
            command_timeout_seconds=command_timeout,
            workdir=workdir,
            auto_pause=True,
        )
    except SandboxPolicyError as exc:
        raise SandboxLifecycleConfigurationError(str(exc)) from exc


def build_e2b_provider() -> E2BSandboxProvider:
    settings = get_settings()
    api_key = str(settings.e2b_api_key or "")
    if not api_key:
        raise SandboxLifecycleConfigurationError(
            "E2B_API_KEY is required when VMA_SANDBOX_PROVIDER=e2b"
        )
    return E2BSandboxProvider(
        api_key,
        domain=_optional(settings.e2b_domain),
        api_url=_optional(settings.e2b_api_url),
        sandbox_url=_optional(settings.e2b_sandbox_url),
        default_template=_optional(settings.vma_e2b_template),
        timeout=int(settings.vma_e2b_timeout_seconds),
        command_timeout=min(
            int(settings.vma_sandbox_command_timeout_seconds),
            int(settings.vma_e2b_timeout_seconds),
        ),
        guest_user=str(settings.vma_e2b_guest_user),
    )


async def build_session_input_bundle(db: AsyncSession, session, agent_version) -> SandboxInputBundle:
    """Resolve the pinned session resources without exposing provider secrets."""
    from app.runtime.runner import _effective_agent_version, _runtime_context_for_session

    effective_version = _effective_agent_version(agent_version, session.status_details)
    runtime_context = await _runtime_context_for_session(db, session, effective_version)
    try:
        return sandbox_input_bundle(
            runtime_context,
            reject_unsupported_resources=True,
        )
    except SandboxInputError as exc:
        raise SandboxLifecycleConfigurationError(str(exc)) from exc


async def provision_session_sandbox(
    db: AsyncSession,
    *,
    session,
    agent_version,
    environment_config: dict[str, Any] | None,
) -> bool:
    """Provision, seed, seal, and pause an E2B sandbox exactly once."""
    if selected_sandbox_provider(environment_config) != E2B_PROVIDER:
        return False
    existing = await sandboxes_q.get_session_sandbox(
        db,
        session.id,
        workspace_id=session.workspace_id,
        for_update=True,
    )
    if existing is not None:
        raise SandboxLifecycleConfigurationError(
            "This session already has a sandbox binding"
        )

    policy = sandbox_policy_from_environment(environment_config)
    bundle = await build_session_input_bundle(db, session, agent_version)
    _validate_bundle_layout(bundle, policy)
    owner = SandboxOwner(session.workspace_id, session.id)
    provider = build_e2b_provider()
    await sandboxes_q.upsert_session_sandbox(
        db,
        workspace_id=session.workspace_id,
        session_id=session.id,
        provider=provider.name,
        state="provisioning",
        config={
            "input_digest": bundle.input_digest,
            "immutable_manifest": bundle.immutable_manifest,
        },
        capabilities=provider.capabilities.to_dict(),
        expires_at=_expiry(),
    )

    connection: SandboxConnection | None = None
    try:
        connection = await provider.provision(
            owner,
            policy,
            template=_optional(get_settings().vma_e2b_template),
        )
        await provider.bootstrap(
            connection,
            files=bundle.upload_pairs(),
            read_only_paths=tuple(item.path for item in bundle.immutable_files),
            mutable_roots=(policy.workdir, *bundle.mutable_roots),
            digest=bundle.input_digest,
        )
        await provider.pause(connection.reference, owner)
        record_config = {
            **connection.config,
            "input_digest": bundle.input_digest,
            "immutable_manifest": bundle.immutable_manifest,
            "skill_sources": list(bundle.skill_sources),
            "memory_sources": list(bundle.memory_sources),
            "mutable_roots": [policy.workdir, *bundle.mutable_roots],
            "configured_template": _optional(get_settings().vma_e2b_template),
            "sealed": True,
        }
        await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=session.workspace_id,
            session_id=session.id,
            provider=connection.reference.provider,
            external_sandbox_id=connection.reference.external_id,
            state="paused",
            template_id=connection.reference.template_id,
            config=record_config,
            capabilities=connection.capabilities,
            error=None,
            last_active_at=_now(),
            expires_at=_expiry(),
        )
        await sessions_q.update_session(
            db,
            session,
            sandbox_state=_public_sandbox_state("paused", policy),
        )
    except BaseException:
        if connection is not None:
            await _best_effort_provider_delete(
                provider,
                connection.reference,
                owner,
                workspace_id=session.workspace_id,
                session_id=session.id,
            )
        raise
    return True


@asynccontextmanager
async def open_e2b_session_backend(
    *,
    workspace_id: str,
    session_id: str,
    environment_config: dict[str, Any] | None,
    input_bundle: SandboxInputBundle,
) -> AsyncIterator[SandboxConnection]:
    """Reconnect the existing sandbox; never seed it from this path."""
    policy = sandbox_policy_from_environment(environment_config)
    record = await _load_record(workspace_id, session_id)
    if record is None or not record.external_sandbox_id:
        raise SandboxLifecycleConfigurationError(
            "E2B sandbox is not provisioned; create a new session"
        )
    if record.provider != E2B_PROVIDER or record.state in {"deleted", "lost"}:
        raise SandboxLifecycleConfigurationError("Session sandbox cannot be resumed")
    expected_digest = str((record.config or {}).get("input_digest") or "")
    if not expected_digest or expected_digest != input_bundle.input_digest:
        raise SandboxInputMismatchError(
            "Session Skills or initial inputs changed; create a new Session"
        )
    expected_manifest = (record.config or {}).get("immutable_manifest")
    if (
        not isinstance(expected_manifest, dict)
        or expected_manifest != input_bundle.immutable_manifest
    ):
        raise SandboxInputMismatchError(
            "Session immutable input manifest changed; create a new Session"
        )
    if (record.config or {}).get("configured_template") != _optional(
        get_settings().vma_e2b_template
    ):
        raise SandboxInputMismatchError(
            "The configured E2B template changed; create a new Session"
        )

    owner = SandboxOwner(workspace_id, session_id)
    reference = _reference_from_record(record, owner)
    provider = build_e2b_provider()
    await _mark_state(workspace_id, session_id, "connecting")
    connection: SandboxConnection | None = None
    try:
        connection = await provider.connect(reference, owner, policy)
        await provider.verify_bootstrap(
            connection,
            digest=expected_digest,
            immutable_manifest=expected_manifest,
        )
        await _mark_state(workspace_id, session_id, "running", last_active_at=_now())
    except BaseException as exc:
        if connection is not None:
            await _best_effort_provider_pause(
                provider,
                reference,
                owner,
                workspace_id=workspace_id,
                session_id=session_id,
            )
        try:
            await _mark_state(
                workspace_id,
                session_id,
                "lost" if isinstance(exc, SandboxNotFoundError) else "error",
                error={"type": type(exc).__name__, "message": str(exc)[:1000]},
            )
        except BaseException:
            logger.exception(
                "session_sandbox_failed_open_state_update_failed",
                workspace_id=workspace_id,
                session_id=session_id,
            )
        raise

    if connection is None:  # pragma: no cover - guarded by successful connect
        raise SandboxLifecycleError("E2B connection was not established")

    failed = False
    try:
        yield connection
    except BaseException:
        failed = True
        raise
    finally:
        try:
            await provider.pause(reference, owner)
            await _mark_state(
                workspace_id,
                session_id,
                "paused",
                error=None,
                last_active_at=_now(),
                expires_at=_expiry(),
            )
        except Exception as exc:
            await _mark_state(
                workspace_id,
                session_id,
                "error",
                error={"type": type(exc).__name__, "message": str(exc)[:1000]},
            )
            if not failed:
                raise


async def pause_session_sandbox(*, workspace_id: str, session_id: str) -> bool:
    target = await _load_target(workspace_id, session_id)
    if target is None:
        return False
    provider, owner, reference, record = target
    if record.state == "paused":
        return True
    await provider.pause(reference, owner)
    await _mark_state(
        workspace_id,
        session_id,
        "paused",
        error=None,
        last_active_at=_now(),
        expires_at=_expiry(),
    )
    return True


async def delete_session_sandbox(
    *,
    workspace_id: str,
    session_id: str,
    only_states: frozenset[str] | None = None,
) -> bool:
    target = await _load_target(workspace_id, session_id)
    if target is None:
        return False
    provider, owner, reference, record = target
    if only_states is not None and record.state not in only_states:
        return False
    try:
        await provider.delete(reference, owner)
    except SandboxNotFoundError:
        pass
    await _mark_state(
        workspace_id,
        session_id,
        "deleted",
        error=None,
        external_sandbox_id=None,
        expires_at=None,
    )
    return True


async def cleanup_expired_session_sandboxes(*, limit: int = 25) -> int:
    async with session_scope() as db:
        candidates = await sandboxes_q.list_expired_session_sandboxes_for_cleanup(
            db,
            limit=limit,
        )
    cleaned = 0
    for record in candidates:
        try:
            if await delete_session_sandbox(
                workspace_id=record.workspace_id,
                session_id=record.session_id,
                only_states=frozenset({"idle", "paused", "error"}),
            ):
                cleaned += 1
        except SandboxProviderError:
            logger.exception(
                "session_sandbox_cleanup_failed",
                workspace_id=record.workspace_id,
                session_id=record.session_id,
            )
    return cleaned


async def run_sandbox_janitor(stop_event: asyncio.Event) -> None:
    interval = max(10, int(get_settings().vma_sandbox_janitor_interval_seconds))
    while not stop_event.is_set():
        try:
            await cleanup_expired_session_sandboxes()
        except Exception:
            logger.exception("session_sandbox_janitor_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue


async def session_has_managed_sandbox(
    db: AsyncSession,
    *,
    workspace_id: str,
    session_id: str,
) -> bool:
    return (
        await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
        )
        is not None
    )


async def bound_session_sandbox_provider(
    *,
    workspace_id: str,
    session_id: str,
) -> str | None:
    """Return the immutable provider binding used to prevent backend downgrade."""
    record = await _load_record(workspace_id, session_id)
    return str(record.provider) if record is not None else None


async def _load_record(workspace_id: str, session_id: str):
    async with session_scope() as db:
        return await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
        )


async def _load_target(workspace_id: str, session_id: str):
    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
        )
        if record is None or not record.external_sandbox_id:
            return None
        session = await sessions_q.get_session(
            db,
            session_id,
            workspace_id=workspace_id,
        )
        if session is None:
            return None
        environment = await environments_q.get_environment(
            db,
            session.environment_id,
            workspace_id=workspace_id,
        )
        if environment is None:
            return None
        owner = SandboxOwner(workspace_id, session_id)
        provider = build_e2b_provider()
        return provider, owner, _reference_from_record(record, owner), record


def _reference_from_record(record, owner: SandboxOwner) -> SandboxReference:
    config = dict(record.config or {})
    external_id = str(record.external_sandbox_id or "")
    if not external_id:
        raise SandboxLifecycleConfigurationError("Sandbox binding has no external ID")
    owner_fingerprint = str(config.get("owner_fingerprint") or "")
    policy_fingerprint = str(config.get("policy_fingerprint") or "")
    if owner_fingerprint != owner.fingerprint:
        raise SandboxLifecycleConfigurationError("Sandbox tenant binding is invalid")
    return SandboxReference(
        provider=str(record.provider),
        external_id=external_id,
        owner_fingerprint=owner_fingerprint,
        policy_fingerprint=policy_fingerprint,
        template_id=record.template_id,
    )


async def _mark_state(
    workspace_id: str,
    session_id: str,
    state: str,
    **values: Any,
) -> None:
    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
            for_update=True,
        )
        if record is None:
            return
        await sandboxes_q.update_session_sandbox_state(
            db,
            record,
            workspace_id=workspace_id,
            state=state,
            **values,
        )
        await db.commit()


async def _best_effort_provider_pause(
    provider: E2BSandboxProvider,
    reference: SandboxReference,
    owner: SandboxOwner,
    *,
    workspace_id: str,
    session_id: str,
) -> None:
    task = asyncio.create_task(provider.pause(reference, owner))
    try:
        async with asyncio.timeout(10):
            await asyncio.shield(task)
    except BaseException:
        logger.exception(
            "session_sandbox_failed_open_pause_failed",
            workspace_id=workspace_id,
            session_id=session_id,
        )


async def _best_effort_provider_delete(
    provider: E2BSandboxProvider,
    reference: SandboxReference,
    owner: SandboxOwner,
    *,
    workspace_id: str,
    session_id: str,
) -> None:
    task = asyncio.create_task(provider.delete(reference, owner))
    try:
        async with asyncio.timeout(10):
            await asyncio.shield(task)
    except BaseException:
        logger.exception(
            "session_sandbox_provision_compensation_failed",
            workspace_id=workspace_id,
            session_id=session_id,
        )


def _validate_bundle_layout(bundle: SandboxInputBundle, policy: SandboxPolicy) -> None:
    mutable_roots = {policy.workdir, *bundle.mutable_roots}
    for item in bundle.files:
        if item.source == "session_file" and not item.read_only:
            raise SandboxLifecycleConfigurationError(
                "E2B initial session files must be read-only"
            )
        if item.source == "session_file" and not item.path.startswith("/mnt/session/"):
            raise SandboxLifecycleConfigurationError(
                "E2B initial session files must be mounted below /mnt/session"
            )
        if item.read_only and any(
            item.path == root or item.path.startswith(root.rstrip("/") + "/")
            for root in mutable_roots
        ):
            raise SandboxLifecycleConfigurationError(
                f"Read-only input {item.path} overlaps mutable workspace/memory; "
                "mount it under /mnt/session/uploads"
            )


def _public_sandbox_state(state: str, policy: SandboxPolicy) -> dict[str, Any]:
    return {
        "enabled": True,
        "backend": "managed_remote",
        "supports_execute": True,
        "policy_enforced": True,
        "lifecycle_state": state,
        "persistence": "one_sandbox_per_session",
        "external_sandbox_id_in_control_plane_response": False,
        "networking": policy.network_access,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry() -> datetime:
    return _now() + timedelta(seconds=max(1, int(get_settings().vma_sandbox_retention_seconds)))


def _optional(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


__all__ = [
    "E2B_PROVIDER",
    "SandboxInputMismatchError",
    "SandboxLifecycleConfigurationError",
    "SandboxLifecycleError",
    "build_session_input_bundle",
    "bound_session_sandbox_provider",
    "cleanup_expired_session_sandboxes",
    "delete_session_sandbox",
    "open_e2b_session_backend",
    "pause_session_sandbox",
    "provision_session_sandbox",
    "run_sandbox_janitor",
    "sandbox_policy_from_environment",
    "selected_sandbox_provider",
    "session_has_managed_sandbox",
]

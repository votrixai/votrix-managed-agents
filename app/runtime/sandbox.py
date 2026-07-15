"""Deep Agents backend selection and sandbox policy boundary."""

from __future__ import annotations

import hashlib
import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import get_settings
from app.runtime.sandbox_inputs import SandboxInputBundle


class SandboxConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxRuntimePlan:
    enabled: bool
    backend: str
    supports_execute: bool
    policy_enforced: bool
    summary: dict[str, Any]


@dataclass(frozen=True)
class BackendHandle:
    backend: Any
    plan: SandboxRuntimePlan
    connection: Any | None = None


def sandbox_plan_from_environment(
    config: dict[str, Any] | None,
    *,
    bound_provider: str | None = None,
) -> SandboxRuntimePlan:
    env_config = dict(config or {})
    sandbox_config = dict(env_config.get("sandbox") or {})
    env_type = str(env_config.get("type") or "cloud")
    settings = get_settings()
    factory = str(settings.vma_sandbox_factory or "").strip()
    unsafe_local = bool(settings.vma_allow_unsafe_local_sandbox)

    if bound_provider is not None:
        if bound_provider != "e2b":
            raise SandboxConfigurationError(
                f"Session is bound to unsupported sandbox provider {bound_provider!r}"
            )
        selected_provider = bound_provider
    else:
        try:
            from app.runtime.sandbox_lifecycle import selected_sandbox_provider

            selected_provider = selected_sandbox_provider(env_config)
        except RuntimeError as exc:
            raise SandboxConfigurationError(str(exc)) from exc

    if bound_provider == "e2b":
        backend = "e2b"
        supports_execute = True
        enforced = True
    elif factory:
        backend = "provider"
        supports_execute = True
        enforced = True
    elif selected_provider == "e2b":
        backend = "e2b"
        supports_execute = True
        enforced = True
    elif env_type == "local" and unsafe_local:
        backend = "unsafe_local_shell"
        supports_execute = True
        enforced = False
    else:
        backend = "langgraph_state"
        supports_execute = False
        enforced = False

    summary: dict[str, Any] = {
        "enabled": True,
        "environment_type": env_type,
        "backend": backend,
        "supports_execute": supports_execute,
        "policy_enforced": enforced,
        "policy": _environment_policy_summary(env_config),
    }
    if backend == "langgraph_state":
        summary["reason"] = (
            "No remote sandbox provider is configured; filesystem state is "
            "checkpointed but shell execution is disabled."
        )
    elif backend == "unsafe_local_shell":
        summary["warning"] = (
            "Unsafe local shell is enabled explicitly; never use this mode for untrusted tenants."
        )
    elif backend == "e2b":
        summary.update(
            {
                "persistence": "one_sandbox_per_session",
                "external_sandbox_id_in_control_plane_response": False,
            }
        )
    if sandbox_config.get("root"):
        summary["virtual_root"] = str(sandbox_config["root"])
    return SandboxRuntimePlan(
        enabled=True,
        backend=backend,
        supports_execute=supports_execute,
        policy_enforced=enforced,
        summary=summary,
    )


@asynccontextmanager
async def open_backend(
    *,
    workspace_id: str,
    session_id: str,
    environment_config: dict[str, Any] | None,
    input_bundle: SandboxInputBundle | None = None,
) -> AsyncIterator[BackendHandle]:
    """Open the session backend through the configured trust boundary."""
    from app.runtime.sandbox_lifecycle import bound_session_sandbox_provider

    bound_provider = await bound_session_sandbox_provider(
        workspace_id=workspace_id,
        session_id=session_id,
    )
    plan = sandbox_plan_from_environment(
        environment_config,
        bound_provider=bound_provider,
    )
    settings = get_settings()
    factory_path = str(settings.vma_sandbox_factory or "").strip()

    if plan.backend == "e2b":
        if input_bundle is None:
            raise SandboxConfigurationError("E2B resume requires the sealed input identity")
        from app.runtime.sandbox_lifecycle import open_e2b_session_backend

        async with open_e2b_session_backend(
            workspace_id=workspace_id,
            session_id=session_id,
            environment_config=environment_config,
            input_bundle=input_bundle,
        ) as connection:
            connected_plan = SandboxRuntimePlan(
                enabled=True,
                backend="e2b",
                supports_execute=True,
                policy_enforced=True,
                summary={
                    **plan.summary,
                    "assignment": "reused",
                    "lifecycle_state": "paused",
                },
            )
            yield BackendHandle(
                backend=connection.backend,
                plan=connected_plan,
                connection=connection,
            )
        return

    if factory_path:
        factory = _load_factory(factory_path)
        produced = factory(
            workspace_id=workspace_id,
            session_id=session_id,
            environment_config=dict(environment_config or {}),
        )
        if hasattr(produced, "__aenter__"):
            async with produced as backend:
                yield BackendHandle(backend=backend, plan=plan)
        else:
            if hasattr(produced, "__await__"):
                produced = await produced
            try:
                yield BackendHandle(backend=produced, plan=plan)
            finally:
                close = getattr(produced, "aclose", None)
                if close is not None:
                    await close()
        return

    if plan.backend == "unsafe_local_shell":
        from deepagents.backends import LocalShellBackend

        root = _local_root(workspace_id, session_id)
        root.mkdir(parents=True, exist_ok=True)
        backend = LocalShellBackend(
            root_dir=root,
            virtual_mode=True,
            inherit_env=False,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        yield BackendHandle(backend=backend, plan=plan)
        return

    from deepagents.backends import StateBackend

    yield BackendHandle(backend=StateBackend(), plan=plan)


def _load_factory(path: str):
    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise SandboxConfigurationError("VMA_SANDBOX_FACTORY must use module:attribute syntax")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise SandboxConfigurationError(f"Sandbox factory is not callable: {path}")
    return factory


def _local_root(workspace_id: str, session_id: str) -> Path:
    settings = get_settings()
    base = Path(str(settings.vma_sandbox_root or "./.vma-sandboxes")).resolve()
    tenant = hashlib.sha256(workspace_id.encode()).hexdigest()[:20]
    session = hashlib.sha256(session_id.encode()).hexdigest()[:24]
    candidate = (base / tenant / session).resolve()
    candidate.relative_to(base)
    return candidate


def _environment_policy_summary(config: dict[str, Any]) -> dict[str, Any]:
    networking = dict(config.get("networking") or {"type": "unrestricted"})
    networking_type = networking.get("type") or "unrestricted"
    if networking_type == "restricted":
        networking_type = "limited"
    networking_summary = {
        "type": networking_type,
        "allowed_hosts": list(networking.get("allowed_hosts") or []),
        "allow_mcp_servers": bool(networking.get("allow_mcp_servers", False)),
        "allow_package_managers": bool(networking.get("allow_package_managers", False)),
    }
    if networking_type == "unrestricted":
        networking_summary["allowed_hosts"] = []

    packages = dict(config.get("packages") or {})
    package_summary = {
        manager: list(packages.get(manager) or [])
        for manager in ("apt", "cargo", "gem", "go", "npm", "pip")
    }
    resources = dict(config.get("resources") or {})
    resource_summary = {
        key: resources[key]
        for key in ("cpu", "memory_mb", "disk_mb", "timeout_seconds")
        if resources.get(key) is not None
    }
    return {
        "networking": networking_summary,
        "packages": package_summary,
        "resources": resource_summary,
    }


__all__ = [
    "BackendHandle",
    "SandboxConfigurationError",
    "SandboxRuntimePlan",
    "open_backend",
    "sandbox_plan_from_environment",
]

"""Deep Agents backend selection and sandbox policy boundary."""

from __future__ import annotations

import hashlib
import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import get_settings


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


def sandbox_plan_from_environment(config: dict[str, Any] | None) -> SandboxRuntimePlan:
    env_config = dict(config or {})
    sandbox_config = dict(env_config.get("sandbox") or {})
    env_type = str(env_config.get("type") or "cloud")
    factory = str(getattr(get_settings(), "vma_sandbox_factory", "") or "").strip()
    unsafe_local = bool(getattr(get_settings(), "vma_allow_unsafe_local_sandbox", False))

    if factory:
        backend = "provider"
        enabled = True
        supports_execute = True
        enforced = True
    elif env_type == "local" and unsafe_local:
        backend = "unsafe_local_shell"
        enabled = True
        supports_execute = True
        enforced = False
    else:
        backend = "langgraph_state"
        enabled = True
        supports_execute = False
        enforced = False

    summary = {
        "enabled": enabled,
        "environment_type": env_type,
        "backend": backend,
        "supports_execute": supports_execute,
        "policy_enforced": enforced,
        "policy": _environment_policy_summary(env_config),
    }
    if backend == "langgraph_state":
        summary["reason"] = "No remote sandbox provider is configured; filesystem state is checkpointed but shell execution is disabled."
    if backend == "unsafe_local_shell":
        summary["warning"] = "Unsafe local shell is enabled explicitly; never use this mode for untrusted tenants."
    if sandbox_config.get("root"):
        summary["virtual_root"] = str(sandbox_config["root"])
    return SandboxRuntimePlan(
        enabled=enabled,
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
) -> AsyncIterator[BackendHandle]:
    """Open a Deep Agents backend through the configured trust boundary."""
    plan = sandbox_plan_from_environment(environment_config)
    settings = get_settings()
    factory_path = str(getattr(settings, "vma_sandbox_factory", "") or "").strip()

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
    base = Path(str(getattr(settings, "vma_sandbox_root", "") or "./.vma-sandboxes")).resolve()
    tenant = hashlib.sha256(workspace_id.encode()).hexdigest()[:20]
    session = hashlib.sha256(session_id.encode()).hexdigest()[:24]
    candidate = (base / tenant / session).resolve()
    candidate.relative_to(base)
    return candidate


def _environment_policy_summary(config: dict[str, Any]) -> dict[str, Any]:
    networking = dict(config.get("networking") or {"type": "unrestricted"})
    networking_type = networking.get("type") or "unrestricted"
    if networking_type in {"restricted", "none"}:
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
    return {"networking": networking_summary, "packages": package_summary, "resources": resource_summary}

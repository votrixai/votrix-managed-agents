from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.db.models import Environment
from app.models.common import ApiModel


class EnvironmentCreateRequest(ApiModel):
    name: str
    description: str | None = None
    config: dict[str, Any] | None = Field(default_factory=lambda: {"type": "cloud"})
    metadata: dict[str, Any] = Field(default_factory=dict)
    scope: Literal["organization", "account"] | None = None

    @model_validator(mode="after")
    def validate_config(self):
        if self.config is not None:
            validate_environment_config(self.config)
        return self


class EnvironmentUpdateRequest(ApiModel):
    name: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    scope: Literal["organization", "account"] | None = None

    @model_validator(mode="after")
    def validate_config(self):
        if self.config is not None:
            validate_environment_config(self.config)
        return self


class EnvironmentResponse(ApiModel):
    id: str
    type: str = "environment"
    name: str
    description: str
    config: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    scope: Literal["organization", "account"] | None = None
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


def validate_environment_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("environment config must be an object")
    env_type = config.get("type", "cloud")
    if env_type not in {"cloud", "self_hosted", "local"}:
        raise ValueError("environment config.type must be cloud, self_hosted, or local")
    _validate_networking(config.get("networking"))
    _validate_packages(config.get("packages"))
    _validate_resources(config.get("resources"))
    _validate_sandbox(config.get("sandbox"))


def _validate_networking(networking: Any) -> None:
    if networking is None:
        return
    if not isinstance(networking, dict):
        raise ValueError("environment config.networking must be an object")
    network_type = networking.get("type")
    if network_type not in {"unrestricted", "restricted", "limited", "none", None}:
        raise ValueError("environment config.networking.type is unsupported")
    allowed_hosts = networking.get("allowed_hosts")
    if allowed_hosts is not None:
        _validate_string_list(allowed_hosts, "environment config.networking.allowed_hosts")
    for key in ("allow_mcp_servers", "allow_package_managers"):
        value = networking.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"environment config.networking.{key} must be a boolean")


def _validate_packages(packages: Any) -> None:
    if packages is None:
        return
    if not isinstance(packages, dict):
        raise ValueError("environment config.packages must be an object")
    for key in ("apt", "cargo", "gem", "go", "npm", "pip"):
        value = packages.get(key)
        if value is not None:
            _validate_string_list(value, f"environment config.packages.{key}")


def _validate_resources(resources: Any) -> None:
    if resources is None:
        return
    if not isinstance(resources, dict):
        raise ValueError("environment config.resources must be an object")
    for key in ("cpu", "memory_mb", "disk_mb", "timeout_seconds"):
        value = resources.get(key)
        if value is not None:
            _validate_positive_number(value, f"environment config.resources.{key}")


def _validate_sandbox(sandbox: Any) -> None:
    if sandbox is None:
        return
    if not isinstance(sandbox, dict):
        raise ValueError("environment config.sandbox must be an object")
    backend = sandbox.get("backend")
    if backend not in {None, "unix_local", "self_hosted_worker", "unconfigured_cloud"}:
        raise ValueError("environment config.sandbox.backend is unsupported")
    root = sandbox.get("root")
    if root is not None and not isinstance(root, str):
        raise ValueError("environment config.sandbox.root must be a string")
    _validate_limit_object(
        sandbox.get("concurrency_limits"),
        "environment config.sandbox.concurrency_limits",
        ("manifest_entries", "local_dir_files"),
    )
    _validate_limit_object(
        sandbox.get("archive_limits"),
        "environment config.sandbox.archive_limits",
        ("max_input_bytes", "max_extracted_bytes", "max_members"),
    )


def _validate_limit_object(value: Any, field: str, allowed_keys: tuple[str, ...]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    for key in allowed_keys:
        item = value.get(key)
        if item is not None:
            _validate_positive_number(item, f"{field}.{key}")


def _validate_positive_number(value: Any, field: str) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive number")


def _validate_string_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be an array of non-empty strings")


def environment_to_response(environment: Environment) -> EnvironmentResponse:
    return EnvironmentResponse(
        id=environment.id,
        name=environment.name,
        description=environment.description,
        config=environment_config_to_response(environment.config),
        metadata=environment.metadata_,
        scope=environment_scope(environment.config),
        archived_at=environment.archived_at,
        deleted_at=environment.deleted_at,
        created_at=environment.created_at,
        updated_at=environment.updated_at,
    )


def environment_config_to_response(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config or {})
    normalized.pop("_scope", None)
    env_type = normalized.get("type", "cloud")
    normalized["type"] = env_type
    if env_type != "cloud":
        return normalized

    networking = normalized.get("networking") or {"type": "unrestricted"}
    if networking.get("type") in {"restricted", "none"}:
        networking = {
            "type": "limited",
            "allowed_hosts": networking.get("allowed_hosts", []),
            "allow_mcp_servers": bool(networking.get("allow_mcp_servers", False)),
            "allow_package_managers": bool(networking.get("allow_package_managers", False)),
        }
    if networking.get("type") == "limited":
        networking = {
            "type": "limited",
            "allowed_hosts": list(networking.get("allowed_hosts") or []),
            "allow_mcp_servers": bool(networking.get("allow_mcp_servers", False)),
            "allow_package_managers": bool(networking.get("allow_package_managers", False)),
        }
    normalized["networking"] = networking

    packages = dict(normalized.get("packages") or {})
    normalized["packages"] = {
        "type": "packages",
        "apt": list(packages.get("apt") or []),
        "cargo": list(packages.get("cargo") or []),
        "gem": list(packages.get("gem") or []),
        "go": list(packages.get("go") or []),
        "npm": list(packages.get("npm") or []),
        "pip": list(packages.get("pip") or []),
    }
    return normalized


def environment_config_with_scope(config: dict[str, Any] | None, scope: str | None) -> dict[str, Any]:
    normalized = dict(config or {"type": "cloud"})
    if scope is not None:
        if normalized.get("type", "cloud") != "self_hosted":
            raise ValueError("environment scope is only supported for self_hosted environments")
        normalized["_scope"] = scope
    return normalized


def environment_scope(config: dict[str, Any] | None) -> Literal["organization", "account"] | None:
    scope = (config or {}).get("_scope")
    if scope in {"organization", "account"}:
        return scope
    return None

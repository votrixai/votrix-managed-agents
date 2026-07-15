from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


PUBLIC_GA_RESOURCES = (
    "api_keys",
    "agents",
    "environments",
    "sessions",
    "files",
    "skills",
    "vaults",
    "model_credentials",
    "model_providers",
)

DEFERRED_CAPABILITIES = (
    "billing_and_invoicing",
    "deployments",
    "dreams",
    "github_repository_resources",
    "memory_writeback",
    "mcp_oauth",
    "presigned_file_uploads",
    "organization_rbac_and_sso",
    "postgres_row_level_security",
    "preview_stream_multi_instance_broker",
    "scheduled_deployments",
    "session_outcomes",
    "session_threads",
    "system_skills",
    "tunnels",
    "user_profiles",
    "webhook_delivery",
)

_VAULT_GA_PATTERNS = (
    re.compile(r"^/v1/vaults$"),
    re.compile(r"^/v1/vaults/[^/]+$"),
    re.compile(r"^/v1/vaults/[^/]+/archive$"),
    re.compile(r"^/v1/vaults/[^/]+/model_credentials(?:/[^/]+(?:/archive)?)?$"),
    re.compile(r"^/v1/vaults/[^/]+/model_credentials/[^/]+/archive$"),
)


def is_public_ga_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    if normalized in {"/", "/health", "/health/db", "/openapi.json", "/v1/capabilities"}:
        return True
    if normalized.startswith("/v1/api_keys"):
        return True
    if normalized.startswith("/v1/agents"):
        return True
    if normalized.startswith("/v1/environments"):
        return "/work" not in normalized
    if normalized.startswith("/v1/sessions"):
        return "/threads" not in normalized
    if normalized.startswith("/v1/files"):
        return normalized not in {"/v1/files/presign", "/v1/files/complete"}
    if normalized.startswith("/v1/skills"):
        return True
    if normalized.startswith("/v1/model_providers"):
        return True
    return any(pattern.fullmatch(normalized) for pattern in _VAULT_GA_PATTERNS)


def public_ga_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    filtered = deepcopy(schema)
    filtered["paths"] = {
        path: value
        for path, value in dict(filtered.get("paths") or {}).items()
        if is_public_ga_path(path)
    }
    info = dict(filtered.get("info") or {})
    info["description"] = (
        "Votrix public beta API. Only generally available, tenant-isolated "
        "resources are included in this schema."
    )
    filtered["info"] = info
    return filtered


def capability_manifest() -> dict[str, Any]:
    return {
        "type": "capability_manifest",
        "release_channel": "public_beta",
        "resources": {name: "ga" for name in PUBLIC_GA_RESOURCES},
        "platform_guarantees": {
            "tenant_scoped_api_keys": "ga",
            "request_ids": "ga",
            "request_rate_limits": "ga",
            "durable_work_leases": "ga",
            "audit_and_usage_ledgers": "ga",
            "tenant_idempotency": "ga",
        },
        "deferred": {name: "unsupported" for name in DEFERRED_CAPABILITIES},
    }

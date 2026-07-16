from __future__ import annotations

import json
import re
import stat
from decimal import Decimal
from pathlib import Path

from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]
SECRET_BASES = {
    "database-url",
    "e2b-api-key",
    "encryption-key",
    "s3-access-key-id",
    "s3-bucket-name",
    "s3-endpoint-url",
    "s3-secret-access-key",
}


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _flatten(value: str) -> str:
    return " ".join(value.split())


def _secret_ref_names(manifest: str) -> list[str]:
    return re.findall(
        r"secretKeyRef:\s*\n(?:\s+[^\n]+\n)*?\s+name:\s*[\"']?([^\s\"'#]+)",
        manifest,
    )


def test_gcp_only_deployment_files_live_at_repo_root() -> None:
    for relative_path in (
        "cloudbuild.yaml",
        "service.production.yaml",
        "service.staging.yaml",
        "scripts/gcloud/config.sh",
        "scripts/gcloud/0-setup-registry.sh",
        "scripts/gcloud/1-create-secrets.sh",
        "scripts/gcloud/2-deploy-production.sh",
        "scripts/gcloud/3-deploy-staging.sh",
        "scripts/gcloud/4-setup-triggers.sh",
        "scripts/gcloud/5-allow-public.sh",
        "scripts/gcloud/status.sh",
    ):
        assert (ROOT / relative_path).is_file(), f"missing GCP deployment file: {relative_path}"

    for relative_path in (
        "deploy/aws",
        "deploy/docker-compose",
        "deploy/fly",
        "deploy/gcp",
        "deploy/railway",
        "deploy/render",
    ):
        legacy_root = ROOT / relative_path
        assert not legacy_root.exists() or not any(path.is_file() for path in legacy_root.rglob("*")), (
            f"legacy deployment target remains: {relative_path}"
        )


def test_cloud_run_manifests_enforce_one_warm_instance_and_health_probes() -> None:
    for environment in ("production", "staging"):
        manifest = _read(f"service.{environment}.yaml")
        flat = _flatten(manifest)

        assert "kind: Service" in manifest
        assert re.search(r'autoscaling\.knative\.dev/maxScale:\s*["\']?1["\']?', manifest)
        assert re.search(r'run\.googleapis\.com/cpu-throttling:\s*["\']?false["\']?', manifest)
        assert "serviceAccountName: vma-runtime@" in manifest
        assert re.search(r"containerConcurrency:\s*40", manifest)
        assert "containerPort: 8080" in manifest
        assert re.search(r"memory:\s*4Gi", manifest)
        assert re.search(r'cpu:\s*["\']?1["\']?', manifest)
        assert "startupProbe:" in manifest and "path: /health" in manifest
        assert "livenessProbe:" in manifest and "path: /health/db" in manifest
        assert "name: WEB_CONCURRENCY" in manifest
        assert re.search(r'name:\s*WEB_CONCURRENCY\s+value:\s*["\']?1["\']?', flat)
        assert "name: RUN_MIGRATIONS" not in manifest
        assert "VMA_RUNTIME_BACKEND" not in manifest
        assert "sqlite" not in manifest.lower()

        assert re.search(
            r'autoscaling\.knative\.dev/minScale:\s*["\']?1["\']?',
            manifest,
        )


def test_cloud_run_secret_names_are_isolated_from_votrix_backend() -> None:
    for environment in ("production", "staging"):
        manifest = _read(f"service.{environment}.yaml")
        names = _secret_ref_names(manifest)
        assert names, f"service.{environment}.yaml must use Secret Manager"
        assert all(name.startswith("vma-") for name in names), names
        suffix = "-staging" if environment == "staging" else ""
        expected_names = {f"vma-{base}{suffix}" for base in SECRET_BASES}
        assert set(names) == expected_names


def test_hosted_runtime_flags_are_explicit_and_consistent() -> None:
    expected = {
        "VMA_EMBEDDED_WORKER_ENABLED": "true",
        "VMA_WORKER_CONCURRENCY": "5",
        "VMA_WORKER_POLL_INTERVAL_SECONDS": "0.5",
        "VMA_WORKER_LEASE_SECONDS": "120",
        "VMA_EVENT_POLL_INTERVAL_SECONDS": "1.0",
        "VMA_MAX_SESSION_INPUT_BYTES": "67108864",
        "VMA_DB_POOL_SIZE": "10",
        "VMA_DB_MAX_OVERFLOW": "5",
        "VMA_DB_POOL_TIMEOUT_SECONDS": "10",
        "VMA_DB_POOL_RECYCLE_SECONDS": "300",
        "VMA_REQUESTS_PER_MINUTE": "600",
        "VMA_MAX_ACTIVE_WORK": "20",
        "VMA_ORGANIZATION_STORAGE_BYTES": "5368709120",
        "VMA_PUBLIC_GA_ONLY": "true",
        "VMA_CORS_ORIGINS": "https://docs.votrixai.com",
    }
    for environment in ("production", "staging"):
        flat = _flatten(_read(f"service.{environment}.yaml"))
        for name, value in expected.items():
            assert re.search(
                rf'name:\s*{re.escape(name)}\s+value:\s*["\']?{re.escape(value)}["\']?',
                flat,
            ), f"{name} is not pinned for {environment}"


def test_hosted_auth_bootstraps_database_keys_without_shared_api_key_secret() -> None:
    deployment_files = (
        ".env.production.example",
        ".env.staging.example",
        "service.production.yaml",
        "service.staging.yaml",
        "scripts/gcloud/1-create-secrets.sh",
        "scripts/gcloud/README.md",
    )
    for relative_path in deployment_files:
        content = _read(relative_path)
        assert "VMA_API_KEY=" not in content
        assert "vma-api-key" not in content

    assert (ROOT / "scripts/bootstrap_api_key.py").is_file()
    readme = _read("scripts/gcloud/README.md")
    assert "python -m scripts.bootstrap_api_key" in readme


def test_cloud_model_registry_is_valid_and_server_controlled() -> None:
    for environment in ("production", "staging"):
        manifest = _read(f"service.{environment}.yaml")
        assert re.search(
            r'- name: VMA_DEFAULT_MODEL_PROVIDER\s+value: ["\']openrouter["\']',
            manifest,
        )
        match = re.search(
            r"- name: VMA_MODEL_PROVIDERS\s+value: '([^']+)'",
            manifest,
        )
        assert match is not None
        registry = json.loads(match.group(1))
        assert set(registry) == {"openrouter"}
        assert registry["openrouter"]["api_key_env"] == "OPENROUTER_API_KEY"
        assert registry["openrouter"]["adapter"] == "openrouter"
        assert registry["openrouter"]["default_model"] == "deepseek/deepseek-v4-pro"
        assert registry["openrouter"]["model_kwargs"] == {
            "openrouter_provider": {
                "order": ["fireworks", "together"],
                "only": ["fireworks", "together"],
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
        assert all("api_key" not in config for config in registry.values())


def test_hosted_runtime_has_no_platform_model_api_key() -> None:
    deployment_files = (
        ".env.production.example",
        ".env.staging.example",
        "service.production.yaml",
        "service.staging.yaml",
        "scripts/gcloud/1-create-secrets.sh",
        "scripts/gcloud/README.md",
    )
    forbidden = (
        "ANTHROPIC_API_KEY=",
        "OPENAI_API_KEY=",
        "DEEPSEEK_API_KEY=",
        "OPENROUTER_API_KEY=",
        "vma-openrouter-api-key",
    )
    for relative_path in deployment_files:
        content = _read(relative_path)
        for value in forbidden:
            assert value not in content, f"{value} remains in {relative_path}"


def test_cloud_e2b_template_resources_match_the_built_profile() -> None:
    for environment in ("production", "staging"):
        manifest = _read(f"service.{environment}.yaml")
        match = re.search(
            r"- name: VMA_E2B_TEMPLATE_RESOURCES\s+value: '([^']+)'",
            manifest,
        )
        assert match is not None
        assert json.loads(match.group(1)) == {"cpu": 2, "memory_mb": 2048}


def test_e2b_cost_estimate_defaults_are_pinned_consistently() -> None:
    expected = {
        "VMA_E2B_COST_ESTIMATION_ENABLED": "true",
        "VMA_E2B_VCPU_SECOND_USD": "0.000014",
        "VMA_E2B_GIB_SECOND_USD": "0.0000045",
    }
    fields = Settings.model_fields
    assert fields["vma_e2b_cost_estimation_enabled"].default is True
    assert fields["vma_e2b_vcpu_second_usd"].default == Decimal(
        expected["VMA_E2B_VCPU_SECOND_USD"]
    )
    assert fields["vma_e2b_gib_second_usd"].default == Decimal(
        expected["VMA_E2B_GIB_SECOND_USD"]
    )

    dotenv = _read(".env.example")
    for name, value in expected.items():
        assert re.search(rf"^{name}={re.escape(value)}$", dotenv, re.MULTILINE)
        for environment in ("production", "staging"):
            manifest = _read(f"service.{environment}.yaml")
            assert re.search(
                rf'- name: {name}\s+value: ["\']{re.escape(value)}["\']',
                manifest,
            )


def test_cloud_build_waits_for_migration_job_before_service_deploy() -> None:
    cloudbuild = _flatten(_read("cloudbuild.yaml"))
    migration_deploy = cloudbuild.find("gcloud run jobs deploy")
    migration_execute = cloudbuild.find("gcloud run jobs execute")
    service_deploy = cloudbuild.find("gcloud run services replace")

    assert migration_deploy >= 0
    assert migration_execute > migration_deploy
    assert "--wait" in cloudbuild[migration_execute:service_deploy]
    assert service_deploy > migration_execute


def test_checkpoint_database_url_is_an_optional_application_override() -> None:
    deployment_files = (
        "cloudbuild.yaml",
        "service.production.yaml",
        "service.staging.yaml",
        "scripts/gcloud/1-create-secrets.sh",
        "scripts/gcloud/2-deploy-production.sh",
        "scripts/gcloud/3-deploy-staging.sh",
    )
    for relative_path in deployment_files:
        content = _read(relative_path)
        assert "VMA_CHECKPOINT_DATABASE_URL" not in content
        assert "vma-checkpoint-database-url" not in content

    example = _read(".env.example")
    assert "VMA_CHECKPOINT_DATABASE_URL=" in example
    assert "derive" in example.lower()


def test_object_storage_has_no_public_bucket_url_configuration() -> None:
    from app.config import Settings

    deployment_files = (
        ".env.example",
        ".env.production.example",
        ".env.staging.example",
        "service.production.yaml",
        "service.staging.yaml",
        "scripts/gcloud/1-create-secrets.sh",
    )
    for relative_path in deployment_files:
        content = _read(relative_path)
        assert "S3_PUBLIC_URL" not in content
        assert "vma-s3-public-url" not in content

    assert "s3_public_url" not in Settings.model_fields


def test_env_examples_separate_required_values_from_optional_overrides() -> None:
    local_example = _read(".env.example")
    required_at = local_example.index("REQUIRED OR CONDITIONALLY REQUIRED")
    optional_at = local_example.index("OPTIONAL OVERRIDES AND TUNING")
    checkpoint_at = local_example.index("VMA_CHECKPOINT_DATABASE_URL=")
    assert required_at < optional_at < checkpoint_at

    for environment in ("production", "staging"):
        example = _read(f".env.{environment}.example")
        required_at = example.index(f"REQUIRED FOR THE STANDARD {environment.upper()} CLOUD RUN PROFILE")
        optional_at = example.index("# OPTIONAL")
        assert required_at < optional_at
        assert "\nVMA_CHECKPOINT_DATABASE_URL=" not in example


def test_local_env_example_covers_every_application_setting() -> None:
    from app.config import Settings

    example_names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", _read(".env.example"), re.MULTILINE))
    setting_names = {name.upper() for name in Settings.model_fields}

    assert setting_names <= example_names, sorted(setting_names - example_names)


def test_storage_quota_uses_only_the_organization_setting() -> None:
    fields = Settings.model_fields
    assert "vma_organization_storage_bytes" in fields
    for relative_path in (
        ".env.example",
        "service.production.yaml",
        "service.staging.yaml",
        "scripts/gcloud/README.md",
    ):
        content = _read(relative_path)
        assert "VMA_ORGANIZATION_STORAGE_BYTES" in content


def test_deepagents_is_fixed_internally_not_selected_by_environment() -> None:
    from app.config import Settings

    runner = _read("app/runtime/runner.py")
    assert "vma_runtime_backend" not in Settings.model_fields
    assert "VMA_RUNTIME_BACKEND" not in _read(".env.example")
    assert "vma_runtime_backend" not in runner
    assert "from app.runtime.deepagents_engine import execute_deep_agent" in runner
    assert "async def _execute_local(" in runner


def test_web_entrypoint_never_runs_migrations() -> None:
    entrypoint = _read("entrypoint.sh")
    assert "alembic" not in entrypoint.lower()
    assert "RUN_MIGRATIONS" not in entrypoint
    assert "votrix_managed_agents:create_app" in entrypoint
    assert "--factory" in entrypoint
    assert '${PORT:-8080}' in entrypoint
    assert '${WEB_CONCURRENCY:-1}' in entrypoint


def test_local_startup_keeps_e2b_runtime_dependencies_installed() -> None:
    run_script = _read("run.sh")
    assert "uv sync --extra sandbox-e2b" in run_script
    assert "--extra dev" in run_script
    assert 'DOCS_PORT="${DOCS_PORT:-4180}"' in run_script
    assert 'node_modules/.bin/next' in run_script
    assert 'Local documentation:' in run_script
    assert '/docs/api/agents/list_agents_v1_agents_get/' in run_script


def test_container_build_is_frozen_and_includes_e2b() -> None:
    dockerfile = _read("Dockerfile")
    assert dockerfile.count("uv sync --frozen --no-dev") == 2
    assert dockerfile.count("--extra sandbox-e2b") == 2
    assert 'CMD ["./entrypoint.sh"]' in dockerfile

    dockerignore = _read(".dockerignore")
    for ignored in (".env.*", "tests", "cloudbuild.yaml", "service.*.yaml", "scripts/gcloud"):
        assert ignored in dockerignore


def test_shell_entrypoints_are_executable() -> None:
    paths = [ROOT / "entrypoint.sh", ROOT / "run.sh", *sorted((ROOT / "scripts").glob("*.sh"))]
    paths.extend(sorted((ROOT / "scripts" / "gcloud").glob("*.sh")))
    assert paths
    for path in paths:
        assert path.stat().st_mode & stat.S_IXUSR, f"not executable: {path.relative_to(ROOT)}"

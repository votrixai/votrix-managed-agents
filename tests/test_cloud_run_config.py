from __future__ import annotations

import json
import re
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_BASES = {
    "api-key",
    "database-url",
    "e2b-api-key",
    "encryption-key",
    "openrouter-api-key",
    "public-base-url",
    "s3-access-key-id",
    "s3-bucket-name",
    "s3-endpoint-url",
    "s3-public-url",
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


def test_cloud_run_manifests_enforce_single_instance_and_health_probes() -> None:
    for environment in ("production", "staging"):
        manifest = _read(f"service.{environment}.yaml")
        flat = _flatten(manifest)

        assert "kind: Service" in manifest
        assert re.search(r'autoscaling\.knative\.dev/maxScale:\s*["\']?1["\']?', manifest)
        assert re.search(r'run\.googleapis\.com/cpu-throttling:\s*["\']?false["\']?', manifest)
        assert "serviceAccountName: vma-runtime@" in manifest
        assert "containerPort: 8080" in manifest
        assert "startupProbe:" in manifest and "path: /health" in manifest
        assert "livenessProbe:" in manifest and "path: /health/db" in manifest
        assert "name: WEB_CONCURRENCY" in manifest
        assert re.search(r'name:\s*WEB_CONCURRENCY\s+value:\s*["\']?1["\']?', flat)
        assert "name: RUN_MIGRATIONS" not in manifest
        assert "VMA_RUNTIME_BACKEND" not in manifest
        assert "sqlite" not in manifest.lower()

        expected_min_scale = "1" if environment == "production" else "0"
        assert re.search(
            rf'autoscaling\.knative\.dev/minScale:\s*["\']?{expected_min_scale}["\']?',
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

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
    "supabase-publishable-key",
    "supabase-url",
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
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
        "scripts/gcloud/config.sh",
        "scripts/gcloud/0-setup-registry.sh",
        "scripts/gcloud/1-create-secrets.sh",
        "scripts/gcloud/2-deploy-production.sh",
        "scripts/gcloud/3-deploy-staging.sh",
        "scripts/gcloud/4-setup-triggers.sh",
        "scripts/gcloud/trigger.yaml.in",
        "scripts/gcloud/5-allow-public.sh",
        "scripts/gcloud/6-bootstrap-operator.sh",
        "scripts/gcloud/7-run-acceptance.sh",
        "scripts/gcloud/8-setup-cloud-tasks.sh",
        "scripts/gcloud/preflight.sh",
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


def test_cloud_run_manifests_split_api_and_worker_roles() -> None:
    api_max_scale = {"production": "3", "staging": "2"}
    worker_scale = {
        "production": ("1", "8"),
        "staging": ("1", "2"),
    }

    for environment in ("production", "staging"):
        manifest = _read(f"service.{environment}.yaml")
        flat = _flatten(manifest)

        assert "kind: Service" in manifest
        assert re.search(
            rf'autoscaling\.knative\.dev/maxScale:\s*["\']?{api_max_scale[environment]}["\']?',
            manifest,
        )
        assert re.search(r'run\.googleapis\.com/cpu-throttling:\s*["\']?false["\']?', manifest)
        assert 'run.googleapis.com/invoker-iam-disabled: "true"' in manifest
        assert "serviceAccountName: vma-runtime@" in manifest
        assert re.search(r"containerConcurrency:\s*40", manifest)
        assert "containerPort: 8080" in manifest
        assert re.search(r"memory:\s*4Gi", manifest)
        assert re.search(r'cpu:\s*["\']?1["\']?', manifest)
        assert "startupProbe:" in manifest and "path: /health" in manifest
        assert "livenessProbe:" in manifest and "path: /health/db" in manifest
        assert "name: WEB_CONCURRENCY" in manifest
        assert re.search(r'name:\s*WEB_CONCURRENCY\s+value:\s*["\']?1["\']?', flat)
        assert re.search(r'name:\s*VMA_SERVICE_ROLE\s+value:\s*["\']?api["\']?', flat)
        assert re.search(r'name:\s*VMA_PREVIEW_BROKER\s+value:\s*["\']?pg_notify["\']?', flat)
        assert re.search(
            r'name:\s*VMA_EMBEDDED_WORKER_ENABLED\s+value:\s*["\']?false["\']?',
            flat,
        )
        expected_queue = "vma-turns" if environment == "production" else "vma-turns-staging"
        expected_dispatch_values = {
            "VMA_WORK_DISPATCH_MODE": "hybrid",
            "VMA_TASKS_QUEUE": expected_queue,
            "VMA_TASKS_LOCATION": "us-central1",
            "VMA_TASKS_SERVICE_ACCOUNT": (
                "vma-runtime@votrixai-480422.iam.gserviceaccount.com"
            ),
            "VMA_WORKER_URL": "__VMA_WORKER_URL__",
        }
        for name, value in expected_dispatch_values.items():
            assert re.search(
                rf'name:\s*{name}\s+value:\s*["\']?{re.escape(value)}["\']?',
                flat,
            )
        for worker_only_name in (
            "VMA_WORKER_CONCURRENCY",
            "VMA_WORKER_POLL_INTERVAL_SECONDS",
            "VMA_WORKER_LEASE_SECONDS",
            "VMA_WORK_MAX_ATTEMPTS",
        ):
            assert f"name: {worker_only_name}" not in manifest
        assert "name: RUN_MIGRATIONS" not in manifest
        assert "VMA_RUNTIME_BACKEND" not in manifest
        assert "sqlite" not in manifest.lower()

        assert re.search(
            r'autoscaling\.knative\.dev/minScale:\s*["\']?1["\']?',
            manifest,
        )

        worker = _read(f"service.worker.{environment}.yaml")
        worker_flat = _flatten(worker)
        min_scale, max_scale = worker_scale[environment]
        expected_name = (
            "votrix-managed-agents-worker"
            if environment == "production"
            else "votrix-managed-agents-staging-worker"
        )
        assert re.search(rf"metadata:\s+name:\s*{expected_name}", worker_flat)
        assert "kind: Service" in worker
        assert re.search(
            rf'autoscaling\.knative\.dev/minScale:\s*["\']?{min_scale}["\']?',
            worker,
        )
        assert re.search(
            rf'autoscaling\.knative\.dev/maxScale:\s*["\']?{max_scale}["\']?',
            worker,
        )
        assert re.search(r'run\.googleapis\.com/cpu-throttling:\s*["\']?false["\']?', worker)
        assert "run.googleapis.com/invoker-iam-disabled" not in worker
        assert "serviceAccountName: vma-runtime@" in worker
        assert re.search(r"containerConcurrency:\s*5", worker)
        assert "startupProbe:" in worker and "path: /health" in worker
        assert "livenessProbe:" in worker and "path: /health/db" in worker
        assert re.search(r'name:\s*WEB_CONCURRENCY\s+value:\s*["\']?1["\']?', worker_flat)
        assert re.search(r'name:\s*VMA_SERVICE_ROLE\s+value:\s*["\']?worker["\']?', worker_flat)
        assert re.search(
            r'name:\s*VMA_PREVIEW_BROKER\s+value:\s*["\']?pg_notify["\']?',
            worker_flat,
        )
        assert re.search(
            r'name:\s*VMA_EMBEDDED_WORKER_ENABLED\s+value:\s*["\']?true["\']?',
            worker_flat,
        )
        expected_worker_values = {
            **expected_dispatch_values,
            "VMA_WORKER_TURN_LIMIT": "5",
            "VMA_WORKER_CONCURRENCY": "1",
            "VMA_WORKER_POLL_INTERVAL_SECONDS": "20",
            "VMA_WORKER_LEASE_SECONDS": "120",
            "VMA_WORK_MAX_ATTEMPTS": "3",
            "VMA_CHECKPOINT_POOL_MAX_SIZE": "5",
            "VMA_DB_POOL_SIZE": "5",
            "VMA_DB_MAX_OVERFLOW": "2",
        }
        for name, value in expected_worker_values.items():
            assert re.search(
                rf'name:\s*{name}\s+value:\s*["\']?{re.escape(value)}["\']?',
                worker_flat,
            )


def test_cloud_run_secret_names_are_isolated_from_votrix_backend() -> None:
    for environment in ("production", "staging"):
        suffix = "-staging" if environment == "staging" else ""
        expected_names = {f"vma-{base}{suffix}" for base in SECRET_BASES}
        api_names = _secret_ref_names(_read(f"service.{environment}.yaml"))
        worker_names = _secret_ref_names(_read(f"service.worker.{environment}.yaml"))
        assert api_names, f"service.{environment}.yaml must use Secret Manager"
        assert all(name.startswith("vma-") for name in api_names), api_names
        assert set(api_names) == expected_names
        assert set(worker_names) == expected_names
        assert set(worker_names) == set(api_names)


def test_hosted_runtime_flags_are_explicit_and_consistent() -> None:
    shared = {
        "VMA_PREVIEW_BROKER": "pg_notify",
        "VMA_EVENT_POLL_INTERVAL_SECONDS": "1.0",
        "VMA_MAX_SESSION_INPUT_BYTES": "67108864",
        "VMA_DB_POOL_TIMEOUT_SECONDS": "10",
        "VMA_DB_POOL_RECYCLE_SECONDS": "300",
        "VMA_REQUESTS_PER_MINUTE": "600",
        "VMA_MAX_ACTIVE_WORK": "20",
        "VMA_ORGANIZATION_STORAGE_BYTES": "5368709120",
        "VMA_PUBLIC_GA_ONLY": "true",
    }
    for environment in ("production", "staging"):
        for role_path in (f"service.{environment}.yaml", f"service.worker.{environment}.yaml"):
            flat = _flatten(_read(role_path))
            for name, value in shared.items():
                assert re.search(
                    rf'name:\s*{re.escape(name)}\s+value:\s*["\']?{re.escape(value)}["\']?',
                    flat,
                ), f"{name} is not pinned for {role_path}"

        browser_origin = (
            "https://staging-app.votrix.ai"
            if environment == "staging"
            else "https://app.votrix.ai"
        )
        expected_cors = f"{browser_origin},https://docs.votrixai.com"
        for role_path in (f"service.{environment}.yaml", f"service.worker.{environment}.yaml"):
            assert re.search(
                rf'name:\s*VMA_CORS_ORIGINS\s+value:\s*["\']{re.escape(expected_cors)}["\']',
                _flatten(_read(role_path)),
            )


def test_hosted_auth_bootstraps_database_keys_without_shared_api_key_secret() -> None:
    deployment_files = (
        ".env.production.example",
        ".env.staging.example",
        "service.production.yaml",
        "service.staging.yaml",
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
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
    assert "6-bootstrap-operator.sh" in readme

    operator_script = _read("scripts/gcloud/6-bootstrap-operator.sh")
    assert "--api-key-stdin" in operator_script
    assert "--redact-secret" in operator_script
    assert "vma-operator-api-key" in operator_script
    assert "--set-secrets" not in operator_script


def test_hosted_user_auth_uses_environment_specific_supabase_secrets() -> None:
    for environment in ("production", "staging"):
        suffix = "-staging" if environment == "staging" else ""
        for path in (f"service.{environment}.yaml", f"service.worker.{environment}.yaml"):
            manifest = _read(path)
            assert "name: VMA_SUPABASE_URL" in manifest
            assert f"name: vma-supabase-url{suffix}" in manifest
            assert "name: VMA_SUPABASE_PUBLISHABLE_KEY" in manifest
            assert f"name: vma-supabase-publishable-key{suffix}" in manifest

    importer = _read("scripts/gcloud/1-create-secrets.sh")
    assert "VMA_SUPABASE_URL|vma-supabase-url" in importer
    assert "VMA_SUPABASE_PUBLISHABLE_KEY|vma-supabase-publishable-key" in importer
    assert '"X-Organization-Id"' in _read("app/factory.py")


def test_cloud_model_registry_is_valid_and_server_controlled() -> None:
    for environment in ("production", "staging"):
        for path in (f"service.{environment}.yaml", f"service.worker.{environment}.yaml"):
            manifest = _read(path)
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
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
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
        for path in (f"service.{environment}.yaml", f"service.worker.{environment}.yaml"):
            manifest = _read(path)
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
            for path in (f"service.{environment}.yaml", f"service.worker.{environment}.yaml"):
                manifest = _read(path)
                assert re.search(
                    rf'- name: {name}\s+value: ["\']{re.escape(value)}["\']',
                    manifest,
                )


def test_cloud_build_waits_for_migration_job_before_service_deploy() -> None:
    cloudbuild = _flatten(_read("cloudbuild.yaml"))
    migration_deploy = cloudbuild.find("gcloud run jobs deploy")
    migration_execute = cloudbuild.find("gcloud run jobs execute")
    service_deploys = [
        match.start() for match in re.finditer("gcloud run services replace", cloudbuild)
    ]

    assert migration_deploy >= 0
    assert migration_execute > migration_deploy
    assert len(service_deploys) == 3
    assert "--wait" in cloudbuild[migration_execute:service_deploys[0]]
    assert all(service_deploy > migration_execute for service_deploy in service_deploys)
    assert cloudbuild.find('Creating $$WORKER_SERVICE in bootstrap poll mode') >= 0
    assert cloudbuild.find('value: "hybrid"|value: "poll"') >= 0
    assert cloudbuild.find('Deploying $$WORKER_SERVICE in hybrid mode') < cloudbuild.find(
        'Deploying ${_SERVICE_NAME} API service in hybrid mode'
    )
    assert cloudbuild.count("__VMA_WORKER_URL__") == 3
    assert 'WORKER_SERVICE_CONFIG="service.worker.${_APP_ENV}.yaml"' in cloudbuild


def test_manual_deploys_use_honest_git_image_tags_and_keep_the_migration_gate() -> None:
    production = _read("scripts/gcloud/2-deploy-production.sh")
    staging = _read("scripts/gcloud/3-deploy-staging.sh")

    assert "status --porcelain --untracked-files=normal" in production
    assert "Production deploys require a clean git worktree." in production
    assert "Production deploys never allow a dirty git worktree." in production
    assert "--short=12 HEAD" in production

    assert "status --porcelain --untracked-files=normal" in staging
    assert "--allow-dirty" in staging
    assert 'ALLOW_DIRTY=false' in staging
    assert '-dirty-$(date -u +%Y%m%d%H%M%S)-$$' in staging
    assert "--short=12 HEAD" in staging

    for script in (production, staging):
        migration_deploy = script.find("gcloud run jobs deploy")
        migration_execute = script.find("gcloud run jobs execute")
        service_deploys = [
            match.start() for match in re.finditer("gcloud run services replace", script)
        ]
        assert migration_deploy >= 0
        assert migration_execute > migration_deploy
        assert len(service_deploys) == 3
        assert "--wait" in script[migration_execute:service_deploys[0]]
        assert all(service_deploy > migration_execute for service_deploy in service_deploys)
        assert 'value: "hybrid"|value: "poll"' in script
        assert "in bootstrap poll mode" in script
        assert "worker service in hybrid mode" in script
        assert "API service in hybrid mode" in script
        assert script.find("worker service in hybrid mode") < script.find(
            "API service in hybrid mode"
        )


def test_cloud_build_trigger_setup_is_safe_idempotent_and_ignores_docs_only_changes() -> None:
    script = _read("scripts/gcloud/4-setup-triggers.sh")
    template = _read("scripts/gcloud/trigger.yaml.in")

    assert '"^main$"' in script
    assert '"^staging$"' in script
    assert "^beta$" not in script
    assert "gcloud builds triggers describe" in script
    assert "gcloud builds triggers import" in script
    assert "gcloud builds triggers update github" not in script
    assert "gcloud builds connections describe" in script
    assert "gcloud builds repositories describe" in script
    assert 'VMA_TRIGGER_REGION:-$REGION' in script
    assert 'VMA_CLOUD_BUILD_CONNECTION:-votrix-github' in script
    assert (
        "VMA_CLOUD_BUILD_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-"
        "compute@developer.gserviceaccount.com" in script
    )
    assert "__SOURCE_REPOSITORY__" in template
    assert "__SERVICE_ACCOUNT__" in template
    assert "repositoryEventConfig:" in template
    assert "--repo-name" not in script
    assert "--repo-owner" not in script
    assert 'VMA_PRODUCTION_TRIGGER_REQUIRE_APPROVAL:-true' in script
    assert "PRODUCTION_APPROVAL_REQUIRED=true" in script
    for ignored_path in (
        "docs/**",
        "website/**",
        "sdks/**",
        "infra/cloudflare/**",
        "README.md",
        "CHANGELOG.md",
    ):
        assert f"  - {ignored_path}" in template
    assert 'VMA_TRIGGER_REGION:-$REGION' in _read("scripts/gcloud/status.sh")


def test_public_access_helper_uses_the_manifest_invoker_check_mode() -> None:
    script = _read("scripts/gcloud/5-allow-public.sh")

    assert script.count("--no-invoker-iam-check") == 1
    assert 'allow_public_api "$PRODUCTION_SERVICE"' in script
    assert 'allow_public_api "$STAGING_SERVICE"' in script
    assert "*worker*)" in script
    assert "Refusing to make a VMA worker service public" in script
    assert "allUsers" not in script
    assert "add-iam-policy-binding" not in script


def test_preflight_is_read_only_and_checks_both_environment_secret_sets() -> None:
    script = _read("scripts/gcloud/preflight.sh")

    assert "secrets versions access" not in script
    assert "secrets versions list" in script
    assert "roles/secretmanager.secretAccessor" in script
    assert "roles/iam.serviceAccountUser" in script
    assert "roles/cloudtasks.enqueuer" in script
    assert "roles/cloudtasks.serviceAgent" in script
    assert "roles/run.invoker" in script
    assert "gcp-sa-cloudtasks.iam.gserviceaccount.com" in script
    assert "cloudtasks.googleapis.com" in script
    assert 'check_tasks_environment "$PRODUCTION_TASKS_QUEUE"' in script
    assert 'check_tasks_environment "$STAGING_TASKS_QUEUE"' in script
    assert "retryConfig.maxAttempts" in script
    assert "rateLimits.maxConcurrentDispatches" in script
    assert "value(format)" in script
    assert "value(disabled)" in script
    assert "--dry-run" in script
    assert 'check_environment_secrets ""' in script
    assert 'check_environment_secrets "-staging"' in script
    for manifest in (
        "service.production.yaml",
        "service.worker.production.yaml",
        "service.staging.yaml",
        "service.worker.staging.yaml",
    ):
        assert f"check_manifest {manifest}" in script
    assert "check_worker_manifest_is_private" in script
    assert "ls-remote --exit-code --heads origin staging" in script


def test_status_reports_missing_resources_instead_of_being_silent() -> None:
    script = _read("scripts/gcloud/status.sh")

    assert "Status: not deployed" in script
    assert '"$PRODUCTION_SERVICE"' in script
    assert '"$PRODUCTION_WORKER_SERVICE"' in script
    assert '"$STAGING_SERVICE"' in script
    assert '"$STAGING_WORKER_SERVICE"' in script
    assert "vma-deploy-production vma-deploy-staging" in script
    assert '"$PRODUCTION_TASKS_QUEUE"' in script
    assert '"$STAGING_TASKS_QUEUE"' in script
    assert "retryConfig.maxAttempts" in script
    assert "rateLimits.maxConcurrentDispatches" in script
    assert "Status: not configured" in script


def test_cloud_tasks_setup_is_idempotent_and_keeps_task_deadline_in_runtime() -> None:
    setup = _read("scripts/gcloud/8-setup-cloud-tasks.sh")
    registry = _read("scripts/gcloud/0-setup-registry.sh")
    config = _read("scripts/gcloud/config.sh")

    assert "cloudtasks.googleapis.com" in registry
    assert 'PRODUCTION_TASKS_QUEUE="vma-turns"' in config
    assert 'STAGING_TASKS_QUEUE="vma-turns-staging"' in config
    assert 'TASKS_LOCATION="$REGION"' in config
    assert "gcloud tasks queues describe" in setup
    assert "gcloud tasks queues create" in setup
    assert "gcloud tasks queues update" in setup
    assert "gcloud tasks queues resume" in setup
    assert "--max-attempts=8" in setup
    assert "--min-backoff=5s" in setup
    assert "--max-backoff=300s" in setup
    assert "--max-concurrent-dispatches=100" in setup
    assert "dispatch-deadline" not in setup.lower()
    assert "dispatchDeadline" not in setup
    assert "roles/cloudtasks.enqueuer" in setup
    assert "roles/cloudtasks.serviceAgent" in setup
    assert "roles/iam.serviceAccountUser" in setup
    assert "roles/run.invoker" in setup
    assert "gcloud beta services identity create" in setup
    assert "gcp-sa-cloudtasks.iam.gserviceaccount.com" in setup
    assert "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" in setup
    assert "Worker service is not deployed yet" in setup
    assert "deploy flow grants Invoker after bootstrap" in setup


def test_production_worker_max_scale_is_explicitly_gated_by_connection_budget() -> None:
    runbook = _read("private-docs/scaling-runbook.md")
    production_deploy = _read("scripts/gcloud/2-deploy-production.sh")
    cloudbuild = _read("cloudbuild.yaml")
    preflight = _read("scripts/gcloud/preflight.sh")

    assert "3×16 + 8×12 = 144" in runbook
    assert "Production maxScale=8 release gate" in runbook
    assert "Status: UNMEASURED" in runbook
    assert "first production deploy is blocked" in runbook
    assert "at least 160 connections" in runbook
    assert "maxScale=8` is a checked-in target, not evidence" in runbook
    assert "Status: UNMEASURED" in production_deploy
    assert "Status: UNMEASURED" in cloudbuild
    assert "check_production_connection_gate" in preflight


def test_deploy_paths_grant_worker_invoker_before_enabling_hybrid() -> None:
    cloudbuild = _read("cloudbuild.yaml")
    invoker = cloudbuild.find("gcloud run services add-iam-policy-binding")
    hybrid_worker = cloudbuild.find("Deploying $$WORKER_SERVICE in hybrid mode")
    hybrid_api = cloudbuild.find(
        "Deploying ${_SERVICE_NAME} API service in hybrid mode"
    )
    assert 0 <= invoker < hybrid_worker < hybrid_api

    for script_name in (
        "scripts/gcloud/2-deploy-production.sh",
        "scripts/gcloud/3-deploy-staging.sh",
    ):
        script = _read(script_name)
        invoker = script.find("gcloud run services add-iam-policy-binding")
        hybrid_worker = script.find("worker service in hybrid mode")
        hybrid_api = script.find("API service in hybrid mode")
        assert 0 <= invoker < hybrid_worker < hybrid_api


def test_checkpoint_database_url_is_an_optional_application_override() -> None:
    deployment_files = (
        "cloudbuild.yaml",
        "service.production.yaml",
        "service.staging.yaml",
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
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
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
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
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
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
    for ignored in (
        ".env.*",
        "tests",
        "sdks",
        "node_modules",
        ".next",
        "cloudbuild.yaml",
        "service.*.yaml",
        "scripts/gcloud",
    ):
        assert ignored in dockerignore

    gcloudignore = _read(".gcloudignore")
    for ignored in (
        ".env.*",
        ".venv",
        "tests",
        "docs",
        "website",
        "sdks",
        "node_modules",
        ".next",
        "cloudbuild.yaml",
        "service.*.yaml",
        "scripts/gcloud",
    ):
        assert ignored in gcloudignore


def test_shell_entrypoints_are_executable() -> None:
    paths = [ROOT / "entrypoint.sh", ROOT / "run.sh", *sorted((ROOT / "scripts").glob("*.sh"))]
    paths.extend(sorted((ROOT / "scripts" / "gcloud").glob("*.sh")))
    assert paths
    for path in paths:
        assert path.stat().st_mode & stat.S_IXUSR, f"not executable: {path.relative_to(ROOT)}"

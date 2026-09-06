"""Run the deploy adapter against a recording CLI; no cloud resources touched."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def deploy(tmp_path, app_env="staging", instances="absent", on_demand=True, failure=""):
    scripts = tmp_path / "scripts/gcloud"
    scripts.mkdir(parents=True)
    for name in ("config.sh", "deploy-pubsub.sh"):
        shutil.copyfile(ROOT / "scripts/gcloud" / name, scripts / name)
    shutil.copyfile(ROOT / "worker-pool.yaml", tmp_path / "worker-pool.yaml")
    manifest = (ROOT / f"service.{app_env}.yaml").read_text().replace('value: "cloud"', 'value: "pubsub"')
    if not on_demand:
        manifest = manifest.replace('name: VMA_WORKER_POOL_ON_DEMAND\n              value: "true"',
                                    'name: VMA_WORKER_POOL_ON_DEMAND\n              value: "false"')
    (tmp_path / f"service.{app_env}.yaml").write_text(manifest)
    binary = tmp_path / "bin"
    binary.mkdir()
    gcloud = binary / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
failure = os.environ["DEPLOY_FAILURE"]
if args[:3] == ["run", "services", "describe"]:
    print("https://scaler.example.run.app")
elif args[:3] == ["scheduler", "jobs", "describe"]:
    if failure == "scheduler_read":
        raise SystemExit("permission denied")
    print(json.dumps({"state": "PAUSED" if failure == "paused" else "ENABLED", "schedule": "* * * * *",
        "httpTarget": {"httpMethod": "POST", "uri": "https://scaler.example.run.app/internal/worker-pool/reconcile",
        "oidcToken": {"audience": "wrong" if failure == "audience" else "https://scaler.example.run.app",
        "serviceAccountEmail": "vma-pool-scheduler@votrixai-480422.iam.gserviceaccount.com"}}}))
elif args[:3] == ["pubsub", "subscriptions", "describe"]:
    print("")
elif args[:3] == ["run", "worker-pools", "list"]:
    if failure == "pool_read":
        raise SystemExit("permission denied")
    count = os.environ["DEPLOY_INSTANCES"]
    print(json.dumps([] if count == "absent" else [{"metadata": {"annotations": {"run.googleapis.com/manualInstanceCount": count}}}]))
elif args[:3] in (["run", "worker-pools", "replace"], ["run", "services", "replace"], ["run", "services", "update"]):
    with Path(os.environ["DEPLOY_RECORD"]).open("a") as output:
        output.write(json.dumps({"args": args, "manifest": sys.stdin.read()}) + "\\n")
else:
    raise SystemExit("Unexpected cloud command: " + repr(args))
"""
    )
    gcloud.chmod(0o755)
    record = tmp_path / "record.jsonl"
    result = subprocess.run(
        [
            "sh",
            str(scripts / "deploy-pubsub.sh"),
            app_env,
            "us-east4",
            "example.pkg.dev/vma:abc123",
            "abc123",
            "abc123",
        ],
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "DEPLOY_RECORD": str(record),
            "DEPLOY_INSTANCES": instances,
            "DEPLOY_FAILURE": failure,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    commands = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    return result, commands


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("instances", ["4", "1", "0", "absent"])
@pytest.mark.parametrize("on_demand", [True, False])
def test_deploy_preserves_scaling_and_renders_pool_before_api(tmp_path, app_env, instances, on_demand):
    result, commands = deploy(tmp_path, app_env, instances, on_demand)
    if on_demand and instances == "4":
        assert result.returncode != 0
        assert not commands
        return
    assert result.returncode == 0, result.stderr
    if on_demand:
        assert commands.pop(0)["args"][1:3] == ["services", "update"]
    assert [row["args"][1] for row in commands] == ["worker-pools", "services"]
    assert "__" not in "".join(row["manifest"] for row in commands)
    pool, api = [yaml.safe_load(row["manifest"]) for row in commands]
    assert pool["kind"] == "WorkerPool"
    assert pool["metadata"]["labels"]["cloud.googleapis.com/location"] == "us-east4"
    assert pool["metadata"]["annotations"][
        "run.googleapis.com/manualInstanceCount"
    ] == (str(0 if on_demand else 1) if instances == "absent" else instances)
    for resource in (pool, api):
        container = resource["spec"]["template"]["spec"]["containers"][0]
        env = {var["name"]: var.get("value") for var in container["env"]}
        assert env["TURN_DISPATCH"] == "pubsub"
        assert env["VMA_WORKER_POOL_ON_DEMAND"] == str(on_demand).lower()
        assert env["PUBSUB_TOPIC"] == "vma-turns" + (
            "-staging" if app_env == "staging" else ""
        )
    container = pool["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] + container["args"] == ["python", "-m", "app.worker"]


@pytest.mark.parametrize("failure", ["scheduler_read", "paused", "audience", "pool_read"])
def test_failed_preflight_does_not_activate_or_reset_workers(tmp_path, failure):
    result, commands = deploy(tmp_path, failure=failure)
    assert result.returncode != 0
    assert not commands


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("existing", [True, False])
def test_scaler_setup_is_private_request_billed_and_oidc_targets_it(tmp_path, app_env, existing):
    scripts = tmp_path / "scripts/gcloud"
    scripts.mkdir(parents=True)
    for name in ("config.sh", "10-setup-worker-pool-scaling.sh"):
        shutil.copyfile(ROOT / "scripts/gcloud" / name, scripts / name)
    shutil.copyfile(ROOT / "worker-pool-scaler.yaml", tmp_path / "worker-pool-scaler.yaml")
    binary = tmp_path / "bin"
    binary.mkdir()
    gcloud = binary / "gcloud"
    gcloud.write_text(f"#!{sys.executable}\n" + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
manifest = sys.stdin.read() if args[:3] == ["run", "services", "replace"] else ""
with Path(os.environ["SETUP_RECORD"]).open("a") as output:
    output.write(json.dumps({"args": args, "manifest": manifest}) + "\\n")
existing = os.environ["SETUP_EXISTING"] == "True"
if args[:3] == ["run", "services", "describe"]:
    print("https://scaler.example.run.app" if "-pool-scaler" in args[3] else "example.pkg.dev/vma:abc123")
elif args[:3] == ["run", "services", "list"]:
    print("https://scaler.example.run.app" if existing else "")
elif args[:2] == ["projects", "describe"]:
    print("123456")
elif args[:3] in (["iam", "roles", "list"], ["iam", "service-accounts", "list"], ["scheduler", "jobs", "list"]):
    print("existing" if existing else "")
""")
    gcloud.chmod(0o755)
    record = tmp_path / "setup.jsonl"
    result = subprocess.run(
        ["sh", str(scripts / "10-setup-worker-pool-scaling.sh"), app_env],
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
             "SETUP_RECORD": str(record), "SETUP_EXISTING": str(existing)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(row) for row in record.read_text().splitlines()]
    resource = next(row["manifest"] for row in calls if row["manifest"])
    assert "__" not in resource
    manifest = yaml.safe_load(resource)
    assert manifest["metadata"]["annotations"]["run.googleapis.com/invoker-iam-disabled"] == "false"
    template = manifest["spec"]["template"]
    assert template["metadata"]["annotations"] == {
        "autoscaling.knative.dev/minScale": "0", "autoscaling.knative.dev/maxScale": "1",
        "run.googleapis.com/cpu-throttling": "true",
    }
    container = template["spec"]["containers"][0]
    env = {v["name"]: v.get("value") for v in container["env"]}
    assert env["APP_ENV"] == app_env
    assert env["VMA_DB_POOL_SIZE"] == "1" and env["VMA_DB_MAX_OVERFLOW"] == "0"
    assert "app.scaler:app" in container["args"]
    job = next(row["args"] for row in calls if row["args"][:3] == [
        "scheduler", "jobs", "update" if existing else "create"
    ])
    assert "--uri=https://scaler.example.run.app/internal/worker-pool/reconcile" in job
    assert "--oidc-token-audience=https://scaler.example.run.app" in job
    grants = [row["args"] for row in calls if "--role=roles/run.invoker" in row["args"]]
    assert len(grants) == 1 and grants[0][3].endswith("-pool-scaler")
    if not existing:
        assert any("--update-env-vars=VMA_SCALER_AUDIENCE=https://scaler.example.run.app" in row["args"] for row in calls)

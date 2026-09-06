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


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("instances", ["4", "0", "absent"])
def test_deploy_preserves_scaling_and_renders_pool_before_api(
    tmp_path, app_env, instances
):
    scripts = tmp_path / "scripts/gcloud"
    scripts.mkdir(parents=True)
    for name in ("config.sh", "deploy-pubsub.sh"):
        shutil.copyfile(ROOT / "scripts/gcloud" / name, scripts / name)
    shutil.copyfile(ROOT / "worker-pool.yaml", tmp_path / "worker-pool.yaml")
    manifest = yaml.safe_load((ROOT / f"service.{app_env}.yaml").read_text())
    for variable in manifest["spec"]["template"]["spec"]["containers"][0]["env"]:
        if variable["name"] == "TURN_DISPATCH":
            variable["value"] = "pubsub"
    (tmp_path / f"service.{app_env}.yaml").write_text(yaml.safe_dump(manifest))
    binary = tmp_path / "bin"
    binary.mkdir()
    gcloud = binary / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:3] == ["pubsub", "subscriptions", "describe"]:
    print("")
elif args[:3] == ["run", "worker-pools", "list"]:
    count = os.environ["DEPLOY_INSTANCES"]
    print(json.dumps([] if count == "absent" else [{"metadata": {"annotations": {"run.googleapis.com/manualInstanceCount": count}}}]))
elif args[:3] in (["run", "worker-pools", "replace"], ["run", "services", "replace"]):
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
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in record.read_text().splitlines()]
    assert [row["args"][1] for row in commands] == ["worker-pools", "services"]
    assert "__" not in "".join(row["manifest"] for row in commands)
    pool, api = [yaml.safe_load(row["manifest"]) for row in commands]
    assert pool["kind"] == "WorkerPool"
    assert pool["metadata"]["labels"]["cloud.googleapis.com/location"] == "us-east4"
    assert pool["metadata"]["annotations"][
        "run.googleapis.com/manualInstanceCount"
    ] == ("1" if instances == "absent" else instances)
    for resource in (pool, api):
        container = resource["spec"]["template"]["spec"]["containers"][0]
        env = {var["name"]: var.get("value") for var in container["env"]}
        assert env["TURN_DISPATCH"] == "pubsub"
        assert env["PUBSUB_TOPIC"] == "vma-turns" + (
            "-staging" if app_env == "staging" else ""
        )
    container = pool["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] + container["args"] == ["python", "-m", "app.worker"]

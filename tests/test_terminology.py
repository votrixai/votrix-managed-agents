import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_LOCK_FILES = {
    "bun.lock",
    "bun.lockb",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "Pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
GENERATED_FILES = {
    "infra/cloudflare/vma-api-router/src/worker-configuration.d.ts",
}


def test_tracked_first_party_text_uses_organization_terminology():
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    tracked_paths = result.stdout.decode().rstrip("\0").split("\0")
    legacy_term = b"cus" + b"tomer"
    offenders: list[str] = []

    for relative_path in tracked_paths:
        path = REPOSITORY_ROOT / relative_path
        if (
            path.name in DEPENDENCY_LOCK_FILES
            or relative_path in GENERATED_FILES
            or not path.is_file()
        ):
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            if legacy_term in line.lower():
                offenders.append(f"{relative_path}:{line_number}")

    assert not offenders, (
        "Use explicit Organization, account, API consumer, or end-user terminology: "
        + ", ".join(offenders)
    )

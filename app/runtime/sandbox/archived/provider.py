"""E2B lifecycle provider backed by the official async adapter.

E2B and ``langchain-e2b`` are optional dependencies. VMA owns lifecycle and
authorization: provider auto-resume is disabled and every reconnect is checked
against the persisted session owner and policy fingerprints.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import importlib
import inspect
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from app.runtime.sandbox_providers.base import (
    ResolvedSandboxPolicy,
    SandboxConnection,
    SandboxDependencyError,
    SandboxNotFoundError,
    SandboxOperationError,
    SandboxOwner,
    SandboxOwnershipError,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxProviderCapabilities,
    SandboxReference,
)

if TYPE_CHECKING:
    from app.runtime.sandbox_outputs import DiscoveredSandboxOutput

DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30 * 60
_PROVIDER = "e2b"
_OWNER_METADATA_KEY = "vma_owner_fingerprint"
_POLICY_METADATA_KEY = "vma_policy_fingerprint"
_MANAGED_BY_METADATA_KEY = "vma_managed_by"
_MANAGED_BY = "votrix-managed-agents"
_GUEST_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_SEAL_PATH = "/var/lib/vma/session-inputs.json"
_STAGING_ROOT = "/var/lib/vma/session-input-staging"
_SESSION_UPLOAD_ROOT = "/mnt/session/uploads"
_SESSION_OUTPUT_ROOT = "/mnt/session/outputs"

_GUEST_ATTEST_COMMAND = """
set -eu
PATH=/usr/bin:/bin
export PATH
test "$(/usr/bin/id -un)" = "$VMA_EXPECTED_GUEST"
test "$(/usr/bin/id -u)" -ne 0
test -x /usr/bin/python3
test ! -w /usr/bin
test ! -w /usr/lib
if test -x /usr/bin/sudo && /usr/bin/sudo -n true >/dev/null 2>&1; then
    exit 41
fi
""".strip()

_PREPARE_SCRIPT = """
import json, os, pwd, stat, sys
p = json.loads(sys.argv[1])
guest = pwd.getpwnam(p["guest"])
if guest.pw_uid == 0:
    raise SystemExit("guest must not be root")
def directory(path, mode=0o755, uid=0, gid=0):
    if not path.startswith("/") or os.path.normpath(path) != path or path == "/":
        raise SystemExit("unsafe directory")
    current = "/"
    for part in path.strip("/").split("/"):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("managed path is not a directory")
    os.chown(path, uid, gid)
    os.chmod(path, mode)
for root in p["protected_roots"]:
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("protected root is unsafe")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("protected root is not operator owned")
    if next(os.scandir(root), None) is not None:
        raise SystemExit("protected root must be empty before bootstrap")
for root in p["protected_roots"]:
    directory(root)
for path in p["file_paths"]:
    directory(os.path.dirname(path))
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise SystemExit("managed input path already exists")
for root in p["mutable_roots"]:
    directory(root, 0o700, guest.pw_uid, guest.pw_gid)
    for current, directories, _files in os.walk(root):
        os.chown(current, guest.pw_uid, guest.pw_gid)
        os.chmod(current, 0o700)
directory(os.path.dirname(p["seal_path"]), 0o700)
""".strip()

_SEAL_SCRIPT = """
import hashlib, json, os, pwd, stat, sys
p = json.loads(sys.argv[1])
guest = pwd.getpwnam(p["guest"])
immutable = {item["path"]: item["sha256"] for item in p["files"] if item["read_only"]}
def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            value.update(block)
    return value.hexdigest()
def expected_tree(root):
    files = {path for path in immutable if path.startswith(root + "/")}
    directories = {root}
    for path in files:
        current = os.path.dirname(path)
        while current != root:
            directories.add(current)
            current = os.path.dirname(current)
    return files, directories
def verify_tree(root):
    expected_files, expected_directories = expected_tree(root)
    seen_files = set()
    seen_directories = set()
    stack = [root]
    while stack:
        current = stack.pop()
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise SystemExit("protected directory metadata changed")
        if stat.S_IMODE(info.st_mode) & 0o022 or current not in expected_directories:
            raise SystemExit("protected directory is unmanaged or writable")
        seen_directories.add(current)
        with os.scandir(current) as entries:
            for entry in entries:
                path = os.path.join(current, entry.name)
                item = os.lstat(path)
                if stat.S_ISLNK(item.st_mode):
                    raise SystemExit("protected subtree contains a symlink")
                if stat.S_ISDIR(item.st_mode):
                    if path not in expected_directories:
                        raise SystemExit("protected subtree contains an unmanaged directory")
                    stack.append(path)
                    continue
                if path not in expected_files or not stat.S_ISREG(item.st_mode):
                    raise SystemExit("protected subtree contains an unmanaged file")
                if item.st_uid != 0 or item.st_nlink != 1 or stat.S_IMODE(item.st_mode) & 0o222:
                    raise SystemExit("sealed input metadata changed")
                if digest(path) != immutable[path]:
                    raise SystemExit("sealed input content changed")
                seen_files.add(path)
    if seen_files != expected_files or seen_directories != expected_directories:
        raise SystemExit("protected subtree is incomplete")
for item in p["files"]:
    info = os.lstat(item["path"])
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit("managed input is not a unique regular file")
    if item["read_only"]:
        if digest(item["path"]) != item["sha256"]:
            raise SystemExit("managed input digest mismatch")
        os.chown(item["path"], 0, 0)
        os.chmod(item["path"], 0o444)
    else:
        os.chown(item["path"], guest.pw_uid, guest.pw_gid)
        os.chmod(item["path"], 0o600)
for root in p["protected_roots"]:
    info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("protected root is unsafe")
    for current, directories, _files in os.walk(root, topdown=False, followlinks=False):
        for name in directories:
            path = os.path.join(current, name)
            item = os.lstat(path)
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                raise SystemExit("protected subtree is unsafe")
            os.chown(path, 0, 0)
            os.chmod(path, 0o555)
        os.chown(current, 0, 0)
        os.chmod(current, 0o555)
    verify_tree(root)
seal = {
    "digest": p["digest"],
    "immutable": immutable,
    "protected_roots": p["protected_roots"],
    "revision": p["revision"],
}
encoded = json.dumps(seal, sort_keys=True, separators=(",", ":")).encode()
temporary = p["seal_path"] + ".tmp"
with open(temporary, "wb") as handle:
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary, 0, 0)
os.chmod(temporary, 0o400)
os.replace(temporary, p["seal_path"])
directory_fd = os.open(os.path.dirname(p["seal_path"]), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
""".strip()

_VERIFY_SCRIPT = """
import hashlib, json, os, stat, sys
p = json.loads(sys.argv[1])
def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            value.update(block)
    return value.hexdigest()
def expected_tree(root):
    files = {path for path in p["immutable"] if path.startswith(root + "/")}
    directories = {root}
    for path in files:
        current = os.path.dirname(path)
        while current != root:
            directories.add(current)
            current = os.path.dirname(current)
    return files, directories
info = os.lstat(p["seal_path"])
if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
        info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
    raise SystemExit("sandbox seal is unsafe")
with open(p["seal_path"], "rb") as handle:
    seal = json.loads(handle.read())
if seal.get("digest") != p["digest"]:
    raise SystemExit("sandbox seal digest mismatch")
if seal.get("immutable") != p["immutable"]:
    raise SystemExit("sandbox immutable manifest mismatch")
if seal.get("revision", 0) != p["revision"]:
    raise SystemExit("sandbox immutable revision mismatch")
if seal.get("protected_roots") != p["protected_roots"]:
    raise SystemExit("sandbox protected-root manifest mismatch")
seen_files = set()
for root in p["protected_roots"]:
    expected_files, expected_directories = expected_tree(root)
    seen_directories = set()
    stack = [root]
    while stack:
        current = stack.pop()
        item = os.lstat(current)
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode) or item.st_uid != 0:
            raise SystemExit("protected directory metadata changed")
        if stat.S_IMODE(item.st_mode) & 0o022 or current not in expected_directories:
            raise SystemExit("protected directory is unmanaged or writable")
        seen_directories.add(current)
        with os.scandir(current) as entries:
            for entry in entries:
                path = os.path.join(current, entry.name)
                child = os.lstat(path)
                if stat.S_ISLNK(child.st_mode):
                    raise SystemExit("protected subtree contains a symlink")
                if stat.S_ISDIR(child.st_mode):
                    if path not in expected_directories:
                        raise SystemExit("protected subtree contains an unmanaged directory")
                    stack.append(path)
                    continue
                if path not in expected_files or not stat.S_ISREG(child.st_mode):
                    raise SystemExit("protected subtree contains an unmanaged file")
                if child.st_uid != 0 or child.st_nlink != 1 or stat.S_IMODE(child.st_mode) & 0o222:
                    raise SystemExit("sealed input metadata changed")
                if digest(path) != p["immutable"][path]:
                    raise SystemExit("sealed input content changed")
                seen_files.add(path)
    if seen_directories != expected_directories:
        raise SystemExit("protected subtree directory manifest changed")
if seen_files != set(p["immutable"]):
    raise SystemExit("sealed input file manifest changed")
print("VMA_SEAL_OK")
""".strip()

_APPEND_PREPARE_SCRIPT = """
import json, os, stat, sys
p = json.loads(sys.argv[1])
def seal_state():
    info = os.lstat(p["seal_path"])
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        raise SystemExit("sandbox seal is unsafe")
    with open(p["seal_path"], "rb") as handle:
        seal = json.loads(handle.read())
    old = (seal.get("digest") == p["previous_digest"] and
           seal.get("immutable") == p["previous_manifest"] and
           seal.get("revision", 0) == p["previous_revision"] and
           seal.get("protected_roots") == p["previous_protected_roots"])
    desired = (seal.get("digest") == p["next_digest"] and
               seal.get("immutable") == p["next_manifest"] and
               seal.get("revision", 0) == p["next_revision"] and
               seal.get("protected_roots") == p["next_protected_roots"])
    if not old and not desired:
        raise SystemExit("sandbox seal is not an append CAS predecessor")
def directory(path, mode):
    current = "/"
    for part in path.strip("/").split("/"):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SystemExit("append staging path is unsafe")
    os.chown(path, 0, 0)
    os.chmod(path, mode)
seal_state()
parent = os.path.dirname(p["staging_root"])
parent_info = os.lstat(parent)
if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode) or
        parent_info.st_uid != 0 or stat.S_IMODE(parent_info.st_mode) & 0o077):
    raise SystemExit("append staging parent is not root-isolated")
directory(p["staging_root"], 0o700)
with os.scandir(p["staging_root"]) as entries:
    for entry in entries:
        path = os.path.join(p["staging_root"], entry.name)
        info = os.lstat(path)
        if entry.name != p["staging_name"] or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise SystemExit("append staging directory contains unmanaged data")
        os.unlink(path)
""".strip()

_APPEND_FINALIZE_SCRIPT = """
import hashlib, json, os, stat, sys
p = json.loads(sys.argv[1])
def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            value.update(block)
    return value.hexdigest()
def load_seal():
    info = os.lstat(p["seal_path"])
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        raise SystemExit("sandbox seal is unsafe")
    with open(p["seal_path"], "rb") as handle:
        return json.loads(handle.read())
def matches(seal, prefix):
    return (seal.get("digest") == p[prefix + "_digest"] and
            seal.get("immutable") == p[prefix + "_manifest"] and
            seal.get("revision", 0) == p[prefix + "_revision"] and
            seal.get("protected_roots") == p[prefix + "_protected_roots"])
def expected_tree(root):
    files = {path for path in p["next_manifest"] if path.startswith(root + "/")}
    directories = {root}
    for path in files:
        current = os.path.dirname(path)
        while current != root:
            directories.add(current)
            current = os.path.dirname(current)
    return files, directories
def verify_next_tree():
    seen_files = set()
    for root in p["next_protected_roots"]:
        expected_files, expected_directories = expected_tree(root)
        seen_directories = set()
        stack = [root]
        while stack:
            current = stack.pop()
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                raise SystemExit("protected directory metadata changed")
            if stat.S_IMODE(info.st_mode) & 0o022 or current not in expected_directories:
                raise SystemExit("protected directory is unmanaged or writable")
            seen_directories.add(current)
            with os.scandir(current) as entries:
                for entry in entries:
                    path = os.path.join(current, entry.name)
                    child = os.lstat(path)
                    if stat.S_ISLNK(child.st_mode):
                        raise SystemExit("protected subtree contains a symlink")
                    if stat.S_ISDIR(child.st_mode):
                        if path not in expected_directories:
                            raise SystemExit("protected subtree contains an unmanaged directory")
                        stack.append(path)
                        continue
                    if path not in expected_files or not stat.S_ISREG(child.st_mode):
                        raise SystemExit("protected subtree contains an unmanaged file")
                    if child.st_uid != 0 or child.st_nlink != 1 or stat.S_IMODE(child.st_mode) & 0o222:
                        raise SystemExit("sealed input metadata changed")
                    if digest(path) != p["next_manifest"][path]:
                        raise SystemExit("sealed input content changed")
                    seen_files.add(path)
        if seen_directories != expected_directories:
            raise SystemExit("protected subtree directory manifest changed")
    if seen_files != set(p["next_manifest"]):
        raise SystemExit("sealed input file manifest changed")
def safe_final(path, expected):
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o222):
        raise SystemExit("append target is unsafe")
    if digest(path) != expected:
        raise SystemExit("append target content mismatch")
def recover_final(path, expected):
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
            info.st_uid != 0 or info.st_nlink != 1):
        raise SystemExit("append recovery target is unsafe")
    if digest(path) != expected:
        raise SystemExit("append recovery target content mismatch")
    os.chown(path, 0, 0)
    os.chmod(path, 0o444)
def remove_stage():
    try:
        info = os.lstat(p["staging_path"])
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise SystemExit("append staging file is unsafe")
    os.unlink(p["staging_path"])
seal = load_seal()
if matches(seal, "next"):
    safe_final(p["target_path"], p["target_sha256"])
    verify_next_tree()
    remove_stage()
    print("VMA_APPEND_ALREADY_APPLIED")
    raise SystemExit(0)
if not matches(seal, "previous"):
    raise SystemExit("sandbox seal is not an append CAS predecessor")
stage = os.lstat(p["staging_path"])
if stat.S_ISLNK(stage.st_mode) or not stat.S_ISREG(stage.st_mode) or stage.st_uid != 0 or stage.st_nlink != 1:
    raise SystemExit("append staging file is unsafe")
if digest(p["staging_path"]) != p["target_sha256"]:
    raise SystemExit("append staging content mismatch")
try:
    os.lstat(p["target_path"])
except FileNotFoundError:
    parent = os.path.dirname(p["target_path"])
    current = p["upload_root"]
    for part in p["target_path"][len(p["upload_root"]):].strip("/").split("/")[:-1]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise SystemExit("append target parent is unsafe")
    os.replace(p["staging_path"], p["target_path"])
    os.chown(p["target_path"], 0, 0)
    os.chmod(p["target_path"], 0o444)
    file_fd = os.open(p["target_path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(file_fd)
    finally:
        os.close(file_fd)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
else:
    recover_final(p["target_path"], p["target_sha256"])
    remove_stage()
for root in p["next_protected_roots"]:
    for current, directories, _files in os.walk(root, topdown=False, followlinks=False):
        for name in directories:
            path = os.path.join(current, name)
            item = os.lstat(path)
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                raise SystemExit("protected subtree is unsafe")
            os.chown(path, 0, 0)
            os.chmod(path, 0o555)
        os.chown(current, 0, 0)
        os.chmod(current, 0o555)
verify_next_tree()
next_seal = {
    "digest": p["next_digest"],
    "immutable": p["next_manifest"],
    "protected_roots": p["next_protected_roots"],
    "revision": p["next_revision"],
}
encoded = json.dumps(next_seal, sort_keys=True, separators=(",", ":")).encode()
temporary = p["seal_path"] + ".tmp"
with open(temporary, "wb") as handle:
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary, 0, 0)
os.chmod(temporary, 0o400)
os.replace(temporary, p["seal_path"])
directory_fd = os.open(os.path.dirname(p["seal_path"]), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print("VMA_APPEND_OK")
""".strip()

_DISCOVER_OUTPUTS_SCRIPT = """
import base64, json, mimetypes, os, pwd, stat, sys
p = json.loads(sys.argv[1])
guest = pwd.getpwnam(p["guest"])
root = p["root"]
info = os.lstat(root)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
    raise SystemExit("sandbox output root is unsafe")
if info.st_uid != guest.pw_uid or stat.S_IMODE(info.st_mode) & 0o022:
    raise SystemExit("sandbox output root ownership is unsafe")
with os.scandir(root) as iterator:
    names = sorted(entry.name for entry in iterator)
if len(names) > p["max_files"]:
    raise SystemExit("sandbox output file count exceeds limit")
results = []
total = 0
for name in names:
    if not name or name in (".", "..") or len(name.encode("utf-8")) > 255 or any(ord(char) < 32 for char in name):
        raise SystemExit("sandbox output filename is unsafe")
    path = os.path.join(root, name)
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SystemExit("sandbox output contains a non-regular file")
    if before.st_uid != guest.pw_uid or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022:
        raise SystemExit("sandbox output file metadata is unsafe")
    if before.st_size > p["max_file_bytes"]:
        raise SystemExit("sandbox output file exceeds limit")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SystemExit("sandbox output changed during discovery")
        chunks = []
        remaining = p["max_file_bytes"] + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
        raise SystemExit("sandbox output changed during discovery")
    if (stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode) or
            after.st_uid != guest.pw_uid or after.st_nlink != 1 or
            stat.S_IMODE(after.st_mode) & 0o022):
        raise SystemExit("sandbox output metadata changed during discovery")
    if len(content) != before.st_size or len(content) > p["max_file_bytes"]:
        raise SystemExit("sandbox output file exceeds limit")
    total += len(content)
    if total > p["max_total_bytes"]:
        raise SystemExit("sandbox output batch exceeds limit")
    mime_type = mimetypes.guess_type(name, strict=False)[0]
    if mime_type is not None and (len(mime_type) > 255 or "/" not in mime_type or any(ord(char) < 32 for char in mime_type)):
        mime_type = None
    results.append({
        "path": path,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "mime_type": mime_type,
        "is_regular_file": True,
        "is_symlink": False,
        "hardlink_count": 1,
    })
print(json.dumps(results, sort_keys=True, separators=(",", ":")))
""".strip()


@dataclass(frozen=True, slots=True)
class E2BDependencies:
    """Injectable async E2B bindings used by offline unit tests."""

    sandbox_class: type[Any]
    backend_class: type[Any]
    not_found_errors: tuple[type[BaseException], ...] = ()


DependencyLoader = Callable[[], E2BDependencies]


class _GuestCommands:
    def __init__(self, commands: Any, guest_user: str) -> None:
        self._commands = commands
        self._guest_user = guest_user

    async def run(self, command: str, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        result = self._commands.run(command, **kwargs)
        if not inspect.isawaitable(result):
            raise SandboxDependencyError("E2B guest command result was not awaitable")
        return await result


class _GuestFiles:
    def __init__(self, files: Any, guest_user: str) -> None:
        self._files = files
        self._guest_user = guest_user

    async def get_info(self, path: str, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        return await self._files.get_info(path, **kwargs)

    async def read(self, path: str, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        return await self._files.read(path, **kwargs)

    async def write(self, path: str, data: Any, **kwargs: Any) -> Any:
        kwargs["user"] = self._guest_user
        return await self._files.write(path, data, **kwargs)


class _GuestSandboxView:
    """Expose only guest-bound command and filesystem handles to Deep Agents."""

    def __init__(self, native: Any, guest_user: str) -> None:
        self.native = native
        self.sandbox_id = native.sandbox_id
        self.commands = _GuestCommands(native.commands, guest_user)
        self.files = _GuestFiles(native.files, guest_user)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)


def load_e2b_dependencies() -> E2BDependencies:
    """Lazily import the official asynchronous E2B SDK and adapter."""

    try:
        e2b = importlib.import_module("e2b")
        adapter = importlib.import_module("langchain_e2b")
    except (ImportError, ModuleNotFoundError) as exc:
        raise SandboxDependencyError(
            "E2B sandbox support requires the optional 'sandbox-e2b' dependencies"
        ) from exc

    sandbox_class = getattr(e2b, "AsyncSandbox", None)
    backend_class = getattr(adapter, "AsyncE2BSandbox", None)
    not_found = getattr(e2b, "SandboxNotFoundException", None)
    if not isinstance(sandbox_class, type) or not isinstance(backend_class, type):
        raise SandboxDependencyError(
            "Installed E2B packages do not expose AsyncSandbox and AsyncE2BSandbox"
        )
    not_found_errors = (
        (not_found,)
        if isinstance(not_found, type) and issubclass(not_found, BaseException)
        else ()
    )
    return E2BDependencies(
        sandbox_class=sandbox_class,
        backend_class=backend_class,
        not_found_errors=not_found_errors,
    )


class E2BSandboxProvider:
    """Provision and manage one tenant-bound E2B sandbox per VMA session."""

    name = _PROVIDER
    capabilities = SandboxProviderCapabilities(
        execute=True,
        file_transfer=True,
        persistence=True,
        pause=True,
        network_modes=frozenset({"none", "limited", "unrestricted"}),
        secure_control_plane=True,
    )

    def __init__(
        self,
        api_key: str,
        *,
        domain: str | None = None,
        api_url: str | None = None,
        sandbox_url: str | None = None,
        default_template: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        keep_memory: bool = False,
        guest_user: str = "user",
        dependencies: DependencyLoader | E2BDependencies | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise ValueError("api_key must be a non-empty string without surrounding whitespace")
        _validate_optional_domain(domain)
        _validate_optional_url(api_url, "api_url")
        _validate_optional_url(sandbox_url, "sandbox_url")
        _validate_optional_name(default_template, "default_template")
        if keep_memory is not False:
            raise ValueError("E2B session persistence requires keep_memory=False")
        if (
            not isinstance(guest_user, str)
            or not _GUEST_USER.fullmatch(guest_user)
            or guest_user == "root"
        ):
            raise ValueError("guest_user must be a safe non-root Linux account name")

        defaults = SandboxPolicy(
            timeout_seconds=timeout,
            command_timeout_seconds=command_timeout,
        ).resolved(
            default_timeout_seconds=timeout,
            default_command_timeout_seconds=command_timeout,
        )
        self._api_key = api_key
        self._domain = domain
        self._api_url = api_url
        self._sandbox_url = sandbox_url
        self._default_template = default_template
        self._timeout = defaults.timeout_seconds
        self._command_timeout = defaults.command_timeout_seconds
        self._guest_user = guest_user
        if isinstance(dependencies, E2BDependencies):
            self._load_dependencies: DependencyLoader = lambda: dependencies
        else:
            self._load_dependencies = dependencies or load_e2b_dependencies

    async def provision(
        self,
        owner: SandboxOwner,
        policy: SandboxPolicy,
        *,
        template: str | None = None,
    ) -> SandboxConnection:
        """Create one isolated sandbox and return its Deep Agents backend."""

        resolved = self._resolve_policy(policy)
        requested_template = template if template is not None else self._default_template
        _validate_optional_name(requested_template, "template")
        dependencies = self._dependencies()
        create_kwargs: dict[str, Any] = {
            **self._api_options(),
            "timeout": resolved.timeout_seconds,
            "metadata": self._ownership_metadata(owner, resolved.fingerprint),
            "secure": True,
            "lifecycle": self._lifecycle_options(resolved),
            **self._network_options(resolved),
        }
        if requested_template is not None:
            create_kwargs["template"] = requested_template

        native = await self._sdk_call(
            "provision",
            dependencies,
            lambda: dependencies.sandbox_class.create(**create_kwargs),
        )
        try:
            external_id = self._native_id(native)
            info = await self._native_info(native, dependencies, operation="provision")
            self._verify_info(
                info,
                external_id=external_id,
                owner=owner,
                policy_fingerprint=resolved.fingerprint,
            )
            self._verify_remote_policy(info, resolved)
            provider_template = _info_value(info, "template_id", "templateID")
            reference = SandboxReference(
                provider=self.name,
                external_id=external_id,
                owner_fingerprint=owner.fingerprint,
                policy_fingerprint=resolved.fingerprint,
                template_id=str(provider_template) if provider_template else requested_template,
            )
            return self._connection(native, reference, resolved, dependencies, info)
        except BaseException:
            await self._best_effort_kill(native)
            raise

    async def connect(
        self,
        reference: SandboxReference,
        owner: SandboxOwner,
        policy: SandboxPolicy,
    ) -> SandboxConnection:
        """Reconnect to the exact persisted sandbox, resuming it if paused."""

        resolved = self._resolve_policy(policy)
        reference.assert_access(provider=self.name, owner=owner, policy=resolved)
        dependencies = self._dependencies()

        info = await self._static_info(reference, owner, dependencies, operation="connect")
        self._verify_remote_policy(info, resolved)
        native = await self._sdk_call(
            "connect",
            dependencies,
            lambda: dependencies.sandbox_class.connect(
                reference.external_id,
                timeout=resolved.timeout_seconds,
                **self._api_options(),
            ),
        )
        try:
            if self._native_id(native) != reference.external_id:
                raise SandboxOwnershipError("E2B returned a different opaque sandbox identifier")
            connected_info = await self._native_info(native, dependencies, operation="connect")
            self._verify_info(
                connected_info,
                external_id=reference.external_id,
                owner=owner,
                policy_fingerprint=reference.policy_fingerprint,
                template_id=reference.template_id,
            )
            self._verify_remote_policy(connected_info, resolved)
            return self._connection(native, reference, resolved, dependencies, connected_info)
        except BaseException:
            await self._best_effort_pause(native)
            raise

    async def pause(self, reference: SandboxReference, owner: SandboxOwner) -> None:
        """Pause a session sandbox without resuming it; filesystem-only snapshot."""

        reference.assert_access(provider=self.name, owner=owner)
        dependencies = self._dependencies()
        await self._static_info(reference, owner, dependencies, operation="pause")
        result = await self._sdk_call(
            "pause",
            dependencies,
            lambda: dependencies.sandbox_class.pause(
                reference.external_id,
                keep_memory=False,
                **self._api_options(),
            ),
        )
        if not isinstance(result, bool):
            raise SandboxOperationError("E2B pause returned an invalid result")

    async def delete(self, reference: SandboxReference, owner: SandboxOwner) -> None:
        """Permanently delete a session sandbox without first resuming it."""

        reference.assert_access(provider=self.name, owner=owner)
        dependencies = self._dependencies()
        await self._static_info(reference, owner, dependencies, operation="delete")
        deleted = await self._sdk_call(
            "delete",
            dependencies,
            lambda: dependencies.sandbox_class.kill(
                reference.external_id,
                **self._api_options(),
            ),
        )
        if deleted is not True:
            raise SandboxNotFoundError("E2B sandbox was not found during deletion")

    async def bootstrap(
        self,
        connection: SandboxConnection,
        *,
        files: list[tuple[str, bytes]],
        read_only_paths: tuple[str, ...],
        mutable_roots: tuple[str, ...],
        digest: str,
    ) -> None:
        """Upload initial inputs once as root and seal immutable content."""
        native = connection.native
        if native is None:
            raise SandboxDependencyError("E2B connection does not expose its native sandbox")
        _validate_digest(digest)
        normalized_files = _normalized_files(files)
        read_only = set(read_only_paths)
        if not read_only.issubset(normalized_files):
            raise SandboxPolicyError("read_only_paths must refer to uploaded files")
        normalized_roots = tuple(sorted({_normalized_path(path, directory=True) for path in mutable_roots}))
        protected_roots = _protected_roots(read_only, normalized_roots)

        await self._attest_guest(native, operation="bootstrap guest attestation")
        await self._run_root(
            native,
            _PREPARE_SCRIPT,
            {
                "guest": self._guest_user,
                "mutable_roots": list(normalized_roots),
                "protected_roots": list(protected_roots),
                "file_paths": sorted(normalized_files),
                "seal_path": _SEAL_PATH,
            },
            operation="bootstrap prepare",
        )
        try:
            for path, content in normalized_files.items():
                result = native.files.write(path, content, user="root")
                if not inspect.isawaitable(result):
                    raise SandboxDependencyError("E2B async filesystem write was not awaitable")
                await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxOperationError("E2B bootstrap upload failed") from exc

        entries = [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "read_only": path in read_only,
            }
            for path, content in sorted(normalized_files.items())
        ]
        await self._run_root(
            native,
            _SEAL_SCRIPT,
            {
                "guest": self._guest_user,
                "files": entries,
                "protected_roots": list(protected_roots),
                "digest": digest,
                "revision": 0,
                "seal_path": _SEAL_PATH,
            },
            operation="bootstrap seal",
        )

    async def verify_bootstrap(
        self,
        connection: SandboxConnection,
        *,
        digest: str,
        immutable_manifest: dict[str, str],
        revision: int = 0,
    ) -> None:
        """Fail closed before a turn if immutable sandbox inputs drifted."""
        native = connection.native
        if native is None:
            raise SandboxDependencyError("E2B connection does not expose its native sandbox")
        _validate_digest(digest)
        manifest = _normalized_manifest(immutable_manifest)
        normalized_revision = _normalized_revision(revision, "revision")
        protected_roots = _protected_roots(set(manifest), ())
        await self._attest_guest(native, operation="resume guest attestation")
        await self._run_root(
            native,
            _VERIFY_SCRIPT,
            {
                "digest": digest,
                "immutable": manifest,
                "protected_roots": list(protected_roots),
                "revision": normalized_revision,
                "seal_path": _SEAL_PATH,
            },
            operation="bootstrap verification",
        )

    async def append_immutable_files(
        self,
        connection: SandboxConnection,
        *,
        files: list[tuple[str, bytes]],
        previous_digest: str,
        previous_manifest: dict[str, str],
        next_digest: str,
        next_manifest: dict[str, str],
        previous_revision: int,
        next_revision: int,
    ) -> None:
        """Atomically advance a paused sandbox's append-only immutable seal.

        Exactly one direct upload is accepted.  The root-only finalize script is
        a compare-and-swap over the persisted seal: an exact predecessor is
        advanced, while an already-applied desired seal is treated as a safe
        retry.  Any third state fails closed.
        """

        native = connection.native
        if native is None:
            raise SandboxDependencyError("E2B connection does not expose its native sandbox")
        _validate_digest(previous_digest)
        _validate_digest(next_digest)
        old_manifest = _normalized_manifest(previous_manifest)
        desired_manifest = _normalized_manifest(next_manifest)
        old_revision = _normalized_revision(previous_revision, "previous_revision")
        desired_revision = _normalized_revision(next_revision, "next_revision")
        if desired_revision != old_revision + 1:
            raise SandboxPolicyError("append revision must advance by exactly one")

        normalized_files = _normalized_files(files)
        if len(normalized_files) != 1:
            raise SandboxPolicyError("append must contain exactly one immutable file")
        target_path, content = next(iter(normalized_files.items()))
        if PurePosixPath(target_path).parent != PurePosixPath(_SESSION_UPLOAD_ROOT):
            raise SandboxPolicyError(
                f"append target must be a direct file under {_SESSION_UPLOAD_ROOT}"
            )
        if any(desired_manifest.get(path) != digest for path, digest in old_manifest.items()):
            raise SandboxPolicyError("append manifest changed an existing immutable file")
        additions = set(desired_manifest) - set(old_manifest)
        if additions != {target_path} or len(desired_manifest) != len(old_manifest) + 1:
            raise SandboxPolicyError("append manifest must be a one-entry strict superset")
        target_sha256 = hashlib.sha256(content).hexdigest()
        if desired_manifest[target_path] != target_sha256:
            raise SandboxPolicyError("append content does not match the immutable manifest")

        previous_protected_roots = _protected_roots(set(old_manifest), ())
        next_protected_roots = _protected_roots(set(desired_manifest), ())
        staging_name = f"payload-{target_sha256[:24]}"
        staging_path = f"{_STAGING_ROOT}/{staging_name}"
        payload = {
            "previous_digest": previous_digest,
            "previous_manifest": old_manifest,
            "previous_protected_roots": list(previous_protected_roots),
            "previous_revision": old_revision,
            "next_digest": next_digest,
            "next_manifest": desired_manifest,
            "next_protected_roots": list(next_protected_roots),
            "next_revision": desired_revision,
            "seal_path": _SEAL_PATH,
            "staging_name": staging_name,
            "staging_path": staging_path,
            "staging_root": _STAGING_ROOT,
            "target_path": target_path,
            "target_sha256": target_sha256,
            "upload_root": _SESSION_UPLOAD_ROOT,
        }

        await self._attest_guest(native, operation="append guest attestation")
        await self._run_root(
            native,
            _APPEND_PREPARE_SCRIPT,
            payload,
            operation="append prepare",
        )
        try:
            result = native.files.write(staging_path, content, user="root")
            if not inspect.isawaitable(result):
                raise SandboxDependencyError("E2B async filesystem write was not awaitable")
            await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxOperationError("E2B append staging upload failed") from exc
        await self._run_root(
            native,
            _APPEND_FINALIZE_SCRIPT,
            payload,
            operation="append finalize",
        )
        await self.verify_bootstrap(
            connection,
            digest=next_digest,
            immutable_manifest=desired_manifest,
            revision=desired_revision,
        )

    async def discover_outputs(
        self,
        connection: SandboxConnection,
        *,
        root: str = _SESSION_OUTPUT_ROOT,
        max_files: int,
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> list[DiscoveredSandboxOutput]:
        """Read bounded direct output files through a root-isolated command."""

        from app.runtime.sandbox_outputs import DiscoveredSandboxOutput

        native = connection.native
        if native is None:
            raise SandboxDependencyError("E2B connection does not expose its native sandbox")
        normalized_root = _normalized_path(root, directory=True)
        if normalized_root != _SESSION_OUTPUT_ROOT:
            raise SandboxPolicyError(
                f"sandbox outputs may only be discovered under {_SESSION_OUTPUT_ROOT}"
            )
        limits = {
            "max_files": _positive_limit(max_files, "max_files"),
            "max_file_bytes": _positive_limit(max_file_bytes, "max_file_bytes"),
            "max_total_bytes": _positive_limit(max_total_bytes, "max_total_bytes"),
        }
        await self._attest_guest(native, operation="output discovery guest attestation")
        stdout = await self._run_root_capture(
            native,
            _DISCOVER_OUTPUTS_SCRIPT,
            {
                "guest": self._guest_user,
                "root": normalized_root,
                **limits,
            },
            operation="output discovery",
        )
        try:
            decoded = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SandboxOperationError("E2B output discovery returned invalid JSON") from exc
        if not isinstance(decoded, list) or len(decoded) > limits["max_files"]:
            raise SandboxOperationError("E2B output discovery returned an invalid file list")

        outputs: list[DiscoveredSandboxOutput] = []
        seen_paths: set[str] = set()
        total_bytes = 0
        for item in decoded:
            if not isinstance(item, dict):
                raise SandboxOperationError("E2B output discovery returned invalid metadata")
            path = item.get("path")
            try:
                filename_bytes = (
                    len(PurePosixPath(path).name.encode("utf-8"))
                    if isinstance(path, str)
                    else 0
                )
            except UnicodeEncodeError as exc:
                raise SandboxOperationError(
                    "E2B output discovery returned an unsafe path"
                ) from exc
            if (
                not isinstance(path, str)
                or any(ord(character) < 32 for character in path)
                or filename_bytes > 255
                or PurePosixPath(path).parent != PurePosixPath(normalized_root)
                or path != str(PurePosixPath(path))
                or path in seen_paths
            ):
                raise SandboxOperationError("E2B output discovery returned an unsafe path")
            hardlink_count = item.get("hardlink_count")
            if (
                item.get("is_regular_file") is not True
                or item.get("is_symlink") is not False
                or isinstance(hardlink_count, bool)
                or not isinstance(hardlink_count, int)
                or hardlink_count != 1
            ):
                raise SandboxOperationError("E2B output discovery returned unsafe file metadata")
            encoded = item.get("content_base64")
            if not isinstance(encoded, str):
                raise SandboxOperationError("E2B output discovery returned invalid file content")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SandboxOperationError(
                    "E2B output discovery returned invalid file content"
                ) from exc
            if len(content) > limits["max_file_bytes"]:
                raise SandboxOperationError("E2B output discovery exceeded the per-file limit")
            total_bytes += len(content)
            if total_bytes > limits["max_total_bytes"]:
                raise SandboxOperationError("E2B output discovery exceeded the batch limit")
            mime_type = item.get("mime_type")
            if mime_type is not None and (
                not isinstance(mime_type, str)
                or not mime_type
                or mime_type != mime_type.strip()
                or len(mime_type) > 255
                or "/" not in mime_type
                or any(ord(character) < 32 for character in mime_type)
            ):
                raise SandboxOperationError("E2B output discovery returned an invalid MIME type")
            outputs.append(
                DiscoveredSandboxOutput(
                    path=path,
                    content=content,
                    mime_type=mime_type,
                    is_regular_file=True,
                    is_symlink=False,
                    hardlink_count=1,
                )
            )
            seen_paths.add(path)
        return outputs

    async def _attest_guest(self, native: Any, *, operation: str) -> None:
        commands = getattr(native, "commands", None)
        run = getattr(commands, "run", None)
        if not callable(run):
            raise SandboxDependencyError("E2B sandbox does not expose async commands.run")
        try:
            result = run(
                _GUEST_ATTEST_COMMAND,
                envs={"VMA_EXPECTED_GUEST": self._guest_user},
                timeout=self._command_timeout,
            )
            if not inspect.isawaitable(result):
                raise SandboxDependencyError("E2B guest attestation was not awaitable")
            completed = await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxPolicyError(f"E2B {operation} failed") from exc
        if getattr(completed, "exit_code", None) != 0:
            raise SandboxPolicyError(
                "E2B template guest identity or privilege boundary is unsafe"
            )

    async def _run_root(
        self,
        native: Any,
        script: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> None:
        await self._run_root_capture(native, script, payload, operation=operation)

    async def _run_root_capture(
        self,
        native: Any,
        script: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> str:
        commands = getattr(native, "commands", None)
        run = getattr(commands, "run", None)
        if not callable(run):
            raise SandboxDependencyError("E2B sandbox does not expose async commands.run")
        command = (
            "/usr/bin/env -i PATH=/usr/bin:/bin "
            "/usr/bin/python3 -I -S -c "
            + shlex.quote(script)
            + " "
            + shlex.quote(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        )
        try:
            result = run(
                command,
                user="root",
                timeout=self._command_timeout,
            )
            if not inspect.isawaitable(result):
                raise SandboxDependencyError("E2B async command result was not awaitable")
            completed = await result
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxOperationError(f"E2B {operation} failed") from exc
        exit_code = getattr(completed, "exit_code", None)
        if exit_code != 0:
            raise SandboxOperationError(f"E2B {operation} failed")
        stdout = getattr(completed, "stdout", None)
        if not isinstance(stdout, str):
            raise SandboxOperationError(f"E2B {operation} returned invalid output")
        return stdout.strip()

    def _resolve_policy(self, policy: SandboxPolicy) -> ResolvedSandboxPolicy:
        if not isinstance(policy, SandboxPolicy):
            raise SandboxPolicyError("policy must be a SandboxPolicy")
        resolved = policy.resolved(
            default_timeout_seconds=self._timeout,
            default_command_timeout_seconds=self._command_timeout,
        )
        self.capabilities.validate(resolved)
        return resolved

    def _dependencies(self) -> E2BDependencies:
        try:
            dependencies = self._load_dependencies()
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxDependencyError("Unable to load E2B sandbox dependencies") from exc
        if not isinstance(dependencies, E2BDependencies):
            raise SandboxDependencyError("E2B dependency loader returned invalid bindings")
        for method in ("create", "connect", "get_info", "pause", "kill"):
            if not callable(getattr(dependencies.sandbox_class, method, None)):
                raise SandboxDependencyError(f"E2B async SDK is missing Sandbox.{method}")
        if not isinstance(dependencies.backend_class, type):
            raise SandboxDependencyError("langchain-e2b adapter binding is invalid")
        return dependencies

    def _connection(
        self,
        native: Any,
        reference: SandboxReference,
        policy: ResolvedSandboxPolicy,
        dependencies: E2BDependencies,
        info: Any,
    ) -> SandboxConnection:
        try:
            backend = dependencies.backend_class(
                sandbox=_GuestSandboxView(native, self._guest_user),
                workdir=policy.workdir,
                timeout=policy.command_timeout_seconds,
            )
        except Exception as exc:
            raise SandboxDependencyError(
                "Official langchain-e2b adapter could not wrap the sandbox"
            ) from exc

        from deepagents.backends.protocol import SandboxBackendProtocol

        if not isinstance(backend, SandboxBackendProtocol):
            raise SandboxDependencyError(
                "Official langchain-e2b adapter does not implement SandboxBackendProtocol"
            )
        config: dict[str, Any] = {
            "policy": policy.to_dict(),
            "keep_memory": False,
            "configured_template": self._default_template,
            "domain": self._domain,
            "api_url": self._api_url,
            "sandbox_url": self._sandbox_url,
            "guest_user": self._guest_user,
            **reference.to_config(),
        }
        if reference.template_id is not None:
            config["template_id"] = reference.template_id
        return SandboxConnection(
            reference=reference,
            backend=cast("SandboxBackendProtocol", backend),
            native=native,
            config=config,
            capabilities=self.capabilities.to_dict(),
            metadata={
                "provider": self.name,
                "state": _state_value(_info_value(info, "state")),
            },
        )

    async def _static_info(
        self,
        reference: SandboxReference,
        owner: SandboxOwner,
        dependencies: E2BDependencies,
        *,
        operation: str,
    ) -> Any:
        info = await self._sdk_call(
            f"{operation} ownership verification",
            dependencies,
            lambda: dependencies.sandbox_class.get_info(
                reference.external_id,
                **self._api_options(),
            ),
        )
        self._verify_info(
            info,
            external_id=reference.external_id,
            owner=owner,
            policy_fingerprint=reference.policy_fingerprint,
            template_id=reference.template_id,
        )
        return info

    async def _native_info(
        self,
        native: Any,
        dependencies: E2BDependencies,
        *,
        operation: str,
    ) -> Any:
        get_info = getattr(native, "get_info", None)
        if not callable(get_info):
            raise SandboxDependencyError("E2B sandbox does not expose get_info")
        return await self._sdk_call(
            f"{operation} ownership verification",
            dependencies,
            lambda: get_info(**self._api_options()),
        )

    async def _sdk_call(
        self,
        operation: str,
        dependencies: E2BDependencies,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            result = call()
            if not inspect.isawaitable(result):
                raise SandboxDependencyError(
                    "E2B async SDK returned a non-awaitable lifecycle result"
                )
            return await result
        except (SandboxDependencyError, SandboxNotFoundError):
            raise
        except Exception as exc:
            if dependencies.not_found_errors and isinstance(exc, dependencies.not_found_errors):
                raise SandboxNotFoundError("E2B sandbox was not found") from exc
            raise SandboxOperationError(f"E2B {operation} failed") from exc

    async def _best_effort_kill(self, native: Any) -> None:
        kill = getattr(native, "kill", None)
        if not callable(kill):
            return
        try:
            result = kill(**self._api_options())
            if inspect.isawaitable(result):
                async with asyncio.timeout(10):
                    await result
        except BaseException:
            return

    async def _best_effort_pause(self, native: Any) -> None:
        pause = getattr(native, "pause", None)
        if not callable(pause):
            return
        try:
            result = pause(keep_memory=False, **self._api_options())
            if inspect.isawaitable(result):
                async with asyncio.timeout(10):
                    await asyncio.shield(result)
        except BaseException:
            return

    def _api_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"api_key": self._api_key}
        for name, value in (
            ("domain", self._domain),
            ("api_url", self._api_url),
            ("sandbox_url", self._sandbox_url),
        ):
            if value is not None:
                options[name] = value
        return options

    @staticmethod
    def _network_options(policy: ResolvedSandboxPolicy) -> dict[str, Any]:
        network: dict[str, Any] = {"allow_public_traffic": False}
        if policy.network_access == "unrestricted":
            return {"allow_internet_access": True, "network": network}
        if policy.network_access == "limited":
            network["allow_out"] = list(policy.allowed_egress)
            return {"allow_internet_access": True, "network": network}
        return {"allow_internet_access": False, "network": network}

    @staticmethod
    def _lifecycle_options(policy: ResolvedSandboxPolicy) -> dict[str, Any]:
        on_timeout: str | dict[str, Any]
        if policy.auto_pause:
            on_timeout = {"action": "pause", "keep_memory": False}
        else:
            on_timeout = "kill"
        return {"on_timeout": on_timeout, "auto_resume": False}

    @staticmethod
    def _ownership_metadata(owner: SandboxOwner, policy_fingerprint: str) -> dict[str, str]:
        return {
            _MANAGED_BY_METADATA_KEY: _MANAGED_BY,
            _OWNER_METADATA_KEY: owner.fingerprint,
            _POLICY_METADATA_KEY: policy_fingerprint,
        }

    def _verify_info(
        self,
        info: Any,
        *,
        external_id: str,
        owner: SandboxOwner,
        policy_fingerprint: str,
        template_id: str | None = None,
    ) -> None:
        remote_id = _info_value(info, "sandbox_id", "sandboxID")
        if remote_id != external_id:
            raise SandboxOwnershipError("E2B returned a different opaque sandbox identifier")
        metadata = _info_value(info, "metadata")
        if not isinstance(metadata, dict):
            raise SandboxOwnershipError("E2B sandbox ownership metadata is unavailable")
        expected = self._ownership_metadata(owner, policy_fingerprint)
        if metadata.get(_MANAGED_BY_METADATA_KEY) != expected[_MANAGED_BY_METADATA_KEY]:
            raise SandboxOwnershipError("E2B sandbox is not managed by this control plane")
        if metadata.get(_OWNER_METADATA_KEY) != expected[_OWNER_METADATA_KEY]:
            raise SandboxOwnershipError("E2B sandbox belongs to a different tenant scope")
        if metadata.get(_POLICY_METADATA_KEY) != expected[_POLICY_METADATA_KEY]:
            raise SandboxPolicyError("E2B sandbox policy metadata does not match the reference")
        remote_template = _info_value(info, "template_id", "templateID")
        if template_id is not None and remote_template != template_id:
            raise SandboxPolicyError("E2B sandbox template no longer matches the reference")

    @staticmethod
    def _verify_remote_policy(info: Any, policy: ResolvedSandboxPolicy) -> None:
        expected_internet = policy.network_access != "none"
        actual_internet = _info_value(info, "allow_internet_access", "allowInternetAccess")
        if not isinstance(actual_internet, bool) or actual_internet is not expected_internet:
            raise SandboxPolicyError("E2B did not enforce the requested internet-access policy")

        network = _info_value(info, "network")
        if not isinstance(network, dict) or network.get("allow_public_traffic") is not False:
            raise SandboxPolicyError("E2B public sandbox traffic is not verifiably disabled")
        if policy.network_access == "limited" and sorted(network.get("allow_out") or []) != sorted(
            policy.allowed_egress
        ):
            raise SandboxPolicyError("E2B outbound allowlist does not match the requested policy")

        lifecycle = _info_value(info, "lifecycle")
        expected_on_timeout = "pause" if policy.auto_pause else "kill"
        if (
            not isinstance(lifecycle, dict)
            or lifecycle.get("on_timeout") != expected_on_timeout
            or lifecycle.get("auto_resume") is not False
        ):
            raise SandboxPolicyError("E2B lifecycle policy does not match the requested policy")

    @staticmethod
    def _native_id(native: Any) -> str:
        external_id = getattr(native, "sandbox_id", None)
        if not isinstance(external_id, str) or not external_id or external_id != external_id.strip():
            raise SandboxOperationError("E2B returned an invalid opaque sandbox identifier")
        return external_id


def _info_value(info: Any, *names: str) -> Any:
    for name in names:
        if isinstance(info, dict) and name in info:
            return info[name]
        value = getattr(info, name, None)
        if value is not None:
            return value
    return None


def _state_value(state: Any) -> str:
    value = getattr(state, "value", state)
    return str(value or "running")


def _validate_optional_name(value: str | None, name: str) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")


def _validate_optional_domain(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 253
        or any(char in value for char in ("/", ":", "@", "?", "#"))
    ):
        raise ValueError("domain must be a hostname without a scheme or path")


def _validate_optional_url(value: str | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or value != value.strip() or len(value) > 2048:
        raise ValueError(f"{name} must be a bounded URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTP(S) URL without credentials, query, or fragment")


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise SandboxPolicyError("bootstrap digest must be a SHA-256 identifier")


def _normalized_revision(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SandboxPolicyError(f"{name} must be a non-negative integer")
    return value


def _positive_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SandboxPolicyError(f"{name} must be a positive integer")
    return value


def _normalized_path(value: str, *, directory: bool = False) -> str:
    try:
        encoded_length = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeEncodeError as exc:
        raise SandboxPolicyError("managed sandbox paths must contain valid UTF-8") from exc
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or encoded_length > 4096
        or any(ord(character) < 32 for character in value)
    ):
        raise SandboxPolicyError("managed sandbox paths must be absolute")
    path = PurePosixPath(value)
    normalized = str(path)
    if value != normalized or value == "/" or ".." in path.parts:
        raise SandboxPolicyError("managed sandbox paths must be normalized below root")
    if directory and normalized in {"/skills", "/mnt", "/var", "/home"}:
        raise SandboxPolicyError("mutable roots must not claim a shared system directory")
    return normalized


def _normalized_files(files: list[tuple[str, bytes]]) -> dict[str, bytes]:
    if not isinstance(files, list):
        raise SandboxPolicyError("bootstrap files must be a list")
    result: dict[str, bytes] = {}
    for raw_path, content in files:
        path = _normalized_path(raw_path)
        if not isinstance(content, bytes):
            raise SandboxPolicyError("bootstrap file content must be bytes")
        existing = result.get(path)
        if existing is not None and existing != content:
            raise SandboxPolicyError(f"conflicting bootstrap file content at {path}")
        for other in result:
            if path.startswith(other + "/") or other.startswith(path + "/"):
                raise SandboxPolicyError("bootstrap path is both a file and directory")
        result[path] = content
    return result


def _normalized_manifest(manifest: dict[str, str]) -> dict[str, str]:
    if not isinstance(manifest, dict):
        raise SandboxPolicyError("immutable manifest must be an object")
    result: dict[str, str] = {}
    for raw_path, digest in manifest.items():
        path = _normalized_path(raw_path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SandboxPolicyError("immutable manifest contains an invalid digest")
        result[path] = digest
    return dict(sorted(result.items()))


def _protected_roots(
    read_only_paths: set[str],
    mutable_roots: tuple[str, ...],
) -> tuple[str, ...]:
    roots: set[str] = {_SESSION_UPLOAD_ROOT}
    for path in read_only_paths:
        parts = PurePosixPath(path).parts
        if path.startswith("/skills/custom/"):
            root = "/skills/custom"
        elif path.startswith(_SESSION_UPLOAD_ROOT + "/"):
            root = _SESSION_UPLOAD_ROOT
        elif path.startswith("/mnt/session/"):
            raise SandboxPolicyError(
                f"read-only session inputs must be below {_SESSION_UPLOAD_ROOT}"
            )
        elif path.startswith("/mnt/memory/") and len(parts) >= 4:
            root = "/" + "/".join(parts[1:4])
        else:
            root = str(PurePosixPath(path).parent)
        roots.add(root)
    for root in roots:
        if any(
            root == mutable
            or root.startswith(mutable.rstrip("/") + "/")
            or mutable.startswith(root.rstrip("/") + "/")
            for mutable in mutable_roots
        ):
            raise SandboxPolicyError("read-only inputs overlap a mutable sandbox root")
    return tuple(sorted(roots))


__all__ = ["E2BDependencies", "E2BSandboxProvider", "load_e2b_dependencies"]

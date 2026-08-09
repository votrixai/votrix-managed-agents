"""The installer command itself, against a local object-store stand-in."""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import threading
import time
import zipfile
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app.utils.sandbox import _skill_install_command


REQUIRED_TOOLS = ("bash", "curl", "unzip", "sha256sum", "awk", "find")
pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in REQUIRED_TOOLS),
    reason="the sandbox installer requires its base-image command set",
)


def _package(name: str) -> bytes:
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: test\n---\n",
        )
    return body.getvalue()


class _Objects:
    def __init__(self, content: dict[str, bytes], *, retries: dict[str, int] | None = None):
        self.content = content
        self.retries = retries or {}
        self.attempts: Counter[str] = Counter()
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()


def _serve(objects: _Objects):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            with objects.lock:
                objects.attempts[path] += 1
                attempt = objects.attempts[path]
                objects.active += 1
                objects.maximum_active = max(objects.maximum_active, objects.active)
            try:
                # Long enough that independently started curl jobs overlap.
                time.sleep(0.08)
                if attempt <= objects.retries.get(path, 0):
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                content = objects.content.get(path)
                if content is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                # Lets integrity-mismatch tests exercise all attempts without
                # sleeping; a Retry-After header is harmless on a 200.
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(content)
            finally:
                with objects.lock:
                    objects.active -= 1

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _skill(name: str, content: bytes):
    return SimpleNamespace(
        name=name,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def _run(command: str):
    process = await asyncio.create_subprocess_exec(
        shutil.which("bash") or "bash",
        "-c",
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


async def test_downloads_overlap_and_only_the_failed_skill_retries(tmp_path):
    packages = {f"/skill-{index}": _package(f"skill-{index}") for index in range(9)}
    objects = _Objects(packages, retries={"/skill-4": 2})
    server, thread = _serve(objects)
    try:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        port = server.server_address[1]
        signed = [
            (
                _skill(path.removeprefix("/"), content),
                f"http://127.0.0.1:{port}{path}?token=do-not-log",
            )
            for path, content in packages.items()
        ]

        code, stdout, stderr = await _run(
            _skill_install_command(
                signed,
                skills_dir=str(skills_dir),
                workdir=str(tmp_path),
            )
        )

        assert code == 0, stderr
        assert 1 < objects.maximum_active <= 9
        assert objects.attempts["/skill-4"] == 3
        assert all(
            attempts == (3 if path == "/skill-4" else 1)
            for path, attempts in objects.attempts.items()
        )
        assert len(list(skills_dir.glob("*/SKILL.md"))) == 9
        assert "skills_installed=9 retries=2" in stdout
        assert "token=do-not-log" not in stdout + stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


async def test_permanent_failure_leaves_the_live_directory_empty(tmp_path):
    good = _package("good")
    objects = _Objects({"/good": good})
    server, thread = _serve(objects)
    try:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        port = server.server_address[1]
        signed = [
            (_skill("good", good), f"http://127.0.0.1:{port}/good"),
            (
                SimpleNamespace(name="missing", size_bytes=1, sha256="0" * 64),
                f"http://127.0.0.1:{port}/missing?token=do-not-log",
            ),
        ]

        code, stdout, stderr = await _run(
            _skill_install_command(
                signed,
                skills_dir=str(skills_dir),
                workdir=str(tmp_path),
            )
        )

        assert code == 31
        assert objects.attempts["/missing"] == 1
        assert list(skills_dir.iterdir()) == []
        assert "token=do-not-log" not in stdout + stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


async def test_twenty_skills_never_exceed_ten_downloads(tmp_path):
    packages = {f"/skill-{index}": _package(f"skill-{index}") for index in range(20)}
    objects = _Objects(packages)
    server, thread = _serve(objects)
    try:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        port = server.server_address[1]
        signed = [
            (
                _skill(path.removeprefix("/"), content),
                f"http://127.0.0.1:{port}{path}",
            )
            for path, content in packages.items()
        ]

        code, _, stderr = await _run(
            _skill_install_command(
                signed,
                skills_dir=str(skills_dir),
                workdir=str(tmp_path),
            )
        )

        assert code == 0, stderr
        assert 1 < objects.maximum_active <= 10
        assert len(list(skills_dir.glob("*/SKILL.md"))) == 20
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


async def test_checksum_mismatch_retries_only_that_skill_and_publishes_nothing(tmp_path):
    content = _package("changed")
    objects = _Objects({"/changed": content})
    server, thread = _serve(objects)
    try:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        port = server.server_address[1]
        signed = [
            (
                SimpleNamespace(
                    name="changed",
                    size_bytes=len(content),
                    sha256="0" * 64,
                ),
                f"http://127.0.0.1:{port}/changed",
            )
        ]

        code, _, stderr = await _run(
            _skill_install_command(
                signed,
                skills_dir=str(skills_dir),
                workdir=str(tmp_path),
            )
        )

        assert code == 31, stderr
        assert objects.attempts["/changed"] == 3
        assert list(skills_dir.iterdir()) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

"""The provider boundary between Memory Stores and native E2B Volumes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import clear_settings_cache
from app.utils import sandbox as sandbox_utils
from app.utils import volume as volume_utils
from app.utils.volume import InvalidVolumeBinding, SandboxVolumeMount, Volume


@pytest.fixture
def e2b_settings(monkeypatch):
    monkeypatch.setenv("APP_ENV", "Staging/US")
    monkeypatch.setenv("E2B_API_KEY", "e2b-test-key")
    clear_settings_cache()
    yield
    clear_settings_cache()


async def test_volume_lifecycle_uses_a_deterministic_name_and_stable_id(
    monkeypatch, e2b_settings
):
    calls: list[tuple[str, str, str]] = []

    class NativeVolume:
        volume_id = "vol_123"
        name = "vma-staging-us-memstore-abc123"

        @staticmethod
        async def create(name, **options):
            calls.append(("create", name, options["api_key"]))
            return NativeVolume()

        @staticmethod
        async def destroy(volume_id, **options):
            calls.append(("destroy", volume_id, options["api_key"]))
            return True

    monkeypatch.setattr(volume_utils, "AsyncVolume", NativeVolume)
    store = SimpleNamespace(
        id="memstore_abc123",
        volume_provider="e2b",
        volume_locator=None,
    )

    locator = await Volume.provision(store)
    store.volume_locator = locator
    mount = Volume.mount(store, "/mnt/memory/project")
    await Volume.destroy(store)

    assert locator == {
        "volume_id": "vol_123",
        "volume_name": "vma-staging-us-memstore-abc123",
    }
    assert mount == SandboxVolumeMount(
        mount_path="/mnt/memory/project",
        volume_name="vma-staging-us-memstore-abc123",
    )
    assert calls == [
        ("create", "vma-staging-us-memstore-abc123", "e2b-test-key"),
        ("destroy", "vol_123", "e2b-test-key"),
    ]
async def test_sandbox_creation_passes_native_mount_path_to_volume_name_mapping(
    db, session, monkeypatch, e2b_settings
):
    created_with = {}

    class NativeSandbox:
        sandbox_id = "sbx_with_memory"

        @staticmethod
        async def create(**options):
            created_with.update(options)
            return NativeSandbox()

    async def _prepared(self):
        return None

    monkeypatch.setattr(sandbox_utils, "AsyncSandbox", NativeSandbox)
    monkeypatch.setattr(sandbox_utils.Sandbox, "prepare_directories", _prepared)

    provisioned = await sandbox_utils.Sandbox.provision(
        db,
        session,
        image=sandbox_utils.Image.base(),
        skill_ids=[],
        files=[],
        memory_mounts=[
            SandboxVolumeMount(
                mount_path="/mnt/memory/content-creator",
                volume_name="vma-staging-us-memstore-content",
            )
        ],
    )

    assert provisioned.sandbox_id == "sbx_with_memory"
    assert created_with["volume_mounts"] == {
        "/mnt/memory/content-creator": "vma-staging-us-memstore-content"
    }


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_provisioning_destroys_the_untracked_native_sandbox(
    db, session, monkeypatch, e2b_settings, cleanup_fails
):
    killed: list[str] = []

    class NativeSandbox:
        sandbox_id = "sbx_failed_before_row"

        @staticmethod
        async def create(**options):
            return NativeSandbox()

        async def kill(self):
            killed.append(self.sandbox_id)
            if cleanup_fails:
                raise RuntimeError("cleanup failed")

    async def _prepare_failed(self):
        raise RuntimeError("layout failed")

    monkeypatch.setattr(sandbox_utils, "AsyncSandbox", NativeSandbox)
    monkeypatch.setattr(
        sandbox_utils.Sandbox,
        "prepare_directories",
        _prepare_failed,
    )

    with pytest.raises(RuntimeError, match="layout failed"):
        await sandbox_utils.Sandbox.provision(
            db,
            session,
            image=sandbox_utils.Image.base(),
            skill_ids=[],
            files=[],
        )

    assert killed == ["sbx_failed_before_row"]


async def test_missing_skill_fails_before_a_native_sandbox_is_created(
    db, session, monkeypatch
):
    created = 0

    class NativeSandbox:
        @staticmethod
        async def create(**options):
            nonlocal created
            created += 1
            raise AssertionError("E2B must not be called")

    monkeypatch.setattr(sandbox_utils, "AsyncSandbox", NativeSandbox)

    with pytest.raises(ValueError, match="does not exist"):
        await sandbox_utils.Sandbox.provision(
            db,
            session,
            image=sandbox_utils.Image.base(),
            skill_ids=["skill_missing"],
            files=[],
        )

    assert created == 0

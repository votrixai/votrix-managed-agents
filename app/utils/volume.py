"""Provider operations for the durable Volume behind a Memory Store.

The control plane owns the Memory Store id and lifecycle. E2B owns the bytes.
Keeping that boundary here means Session provisioning never learns how a
provider creates or destroys storage; it only receives a mount descriptor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from e2b import AsyncVolume

from app.config import get_settings
from app.db.models import MemoryStore
from app.db.models.memory import VOLUME_PROVIDER_E2B


class InvalidVolumeBinding(ValueError):
    """The stored provider locator cannot identify a mountable Volume."""


@dataclass(frozen=True)
class SandboxVolumeMount:
    """One native E2B mount, resolved before the Sandbox is created."""

    mount_path: str
    volume_name: str


class Volume:
    """The provider adapter used by Memory Store and Session services."""

    @classmethod
    async def provision(cls, store: MemoryStore) -> dict[str, str]:
        if store.volume_provider != VOLUME_PROVIDER_E2B:
            raise InvalidVolumeBinding(
                f"Volume provider {store.volume_provider!r} is not implemented"
            )
        settings = get_settings()
        if not settings.e2b_api_key.strip():
            raise RuntimeError("E2B_API_KEY is not configured")

        native = await AsyncVolume.create(
            cls.provider_name(store.id),
            api_key=settings.e2b_api_key,
        )
        return {"volume_id": native.volume_id, "volume_name": native.name}

    @classmethod
    async def destroy(cls, store: MemoryStore) -> None:
        if store.volume_provider != VOLUME_PROVIDER_E2B:
            raise InvalidVolumeBinding(
                f"Volume provider {store.volume_provider!r} is not implemented"
            )
        volume_id = cls._locator_value(store, "volume_id")
        settings = get_settings()
        if not settings.e2b_api_key.strip():
            raise RuntimeError("E2B_API_KEY is not configured")
        # E2B returns False for an already absent Volume. That is success for
        # our delete saga: retrying a completed provider side effect must be a
        # no-op rather than a new failure.
        await AsyncVolume.destroy(volume_id, api_key=settings.e2b_api_key)

    @classmethod
    def mount(cls, store: MemoryStore, mount_path: str) -> SandboxVolumeMount:
        if store.volume_provider != VOLUME_PROVIDER_E2B:
            raise InvalidVolumeBinding(
                f"Volume provider {store.volume_provider!r} cannot mount in E2B"
            )
        return SandboxVolumeMount(
            mount_path=mount_path,
            # AsyncSandbox.create sends a Volume name to E2B even when handed
            # an AsyncVolume object, so the name is the actual mount handle.
            volume_name=cls._locator_value(store, "volume_name"),
        )

    @staticmethod
    def provider_name(memory_store_id: str) -> str:
        """A deterministic, environment-separated E2B name.

        E2B permits letters, numbers and hyphens. The public Memory Store id
        contains an underscore, and APP_ENV is operator input, so both are
        normalised before they cross the provider boundary.
        """
        environment = get_settings().app_env or "local"
        raw = f"vma-{environment}-{memory_store_id}".lower()
        return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")

    @staticmethod
    def _locator_value(store: MemoryStore, key: str) -> str:
        locator: dict[str, Any] = store.volume_locator or {}
        value = locator.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InvalidVolumeBinding(
                f"Memory Store {store.id} has no valid {key!r} in volume_locator"
            )
        return value


def memory_mount_path(name: str, memory_store_id: str) -> str:
    """Derive the public mount slug from the display name.

    The id fallback keeps a store with a non-Latin-only display name usable
    without inventing a path that could collide with `/mnt/memory` itself.
    Duplicate paths inside one Session are rejected by the service layer.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = f"memory-{memory_store_id.rsplit('_', 1)[-1][-12:]}"
    return f"/mnt/memory/{slug}"


__all__ = [
    "InvalidVolumeBinding",
    "SandboxVolumeMount",
    "Volume",
    "memory_mount_path",
]

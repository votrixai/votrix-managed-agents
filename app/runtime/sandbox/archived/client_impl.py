"""Minimal E2B sandbox wrapper: create once per session, connect every turn.

Deliberately simplified from the archived provider (see e2b_archived.py):
no immutable-input sealing, no guest attestation, no append-only manifest.
One class, one instance per live connection: ``Sandbox.create()``/``Sandbox.connect()``
return an instance of this class wrapping the real e2b SDK object.
"""

from __future__ import annotations

from typing import Any

from e2b import AsyncSandbox

_DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes


class Sandbox:
    """One instance = one connected E2B sandbox."""

    def __init__(self, native: AsyncSandbox, api_key: str, guest_user: str, timeout_seconds: int) -> None:
        self._native = native
        self._api_key = api_key
        self._guest_user = guest_user
        self._timeout_seconds = timeout_seconds

    @property
    def sandbox_id(self) -> str:
        return self._native.sandbox_id

    # ---- 1. create -------------------------------------------------
    @classmethod
    async def create(
        cls,
        api_key: str,
        template: str,
        guest_user: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> Sandbox:
        native = await AsyncSandbox.create(
            template=template,
            timeout=timeout_seconds,
            api_key=api_key,
            lifecycle={
                "on_timeout": {"action": "pause", "keep_memory": False},
                "auto_resume": False,
            },
        )
        return cls(native, api_key, guest_user, timeout_seconds)

    # ---- 2. connect --------------------------------------------------
    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        api_key: str,
        guest_user: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> Sandbox:
        native = await AsyncSandbox.connect(
            sandbox_id,
            timeout=timeout_seconds,
            api_key=api_key,
        )
        return cls(native, api_key, guest_user, timeout_seconds)

    # ---- lifecycle -----------------------------------------------------
    async def pause(self) -> None:
        await self._native.pause(keep_memory=False)

    async def kill(self) -> None:
        await self._native.kill()

    # ---- files -----------------------------------------------------------
    async def upload_file(self, path: str, content: bytes) -> None:
        await self._native.files.write(path, content, user=self._guest_user)

    async def download_file(self, path: str) -> bytes:
        return await self._native.files.read(path, format="bytes", user=self._guest_user)

    # ---- commands ----------------------------------------------------------
    async def run(self, command: str, **kwargs: Any) -> Any:
        await self._native.set_timeout(self._timeout_seconds)
        kwargs.setdefault("user", self._guest_user)
        return await self._native.commands.run(command, **kwargs)


__all__ = ["Sandbox"]

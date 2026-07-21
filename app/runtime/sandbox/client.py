"""Sandbox client: create once per session, connect every turn.

Interface only — no implementation yet. See archived/client_impl.py for the
previous working version this replaces (create/connect/pause/kill/upload/
download/run against the raw e2b SDK, no sealing, no attestation).
"""

from __future__ import annotations

from typing import Any

from e2b import AsyncSandbox

_DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes


class Sandbox:
    """One instance = one connected E2B sandbox."""

    def __init__(self, native: AsyncSandbox, api_key: str, guest_user: str, timeout_seconds: int) -> None: ...

    @property
    def sandbox_id(self) -> str: ...

    @property
    def native(self) -> AsyncSandbox:
        """The underlying e2b SDK object — for wrapping by sandbox/backend.py."""
        ...

    # ---- 1. create -------------------------------------------------
    @classmethod
    async def create(
        cls,
        api_key: str,
        template: str,
        guest_user: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> Sandbox: ...

    # ---- 2. connect --------------------------------------------------
    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        api_key: str,
        guest_user: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> Sandbox: ...

    # ---- lifecycle -----------------------------------------------------
    async def pause(self) -> None: ...

    async def kill(self) -> None: ...

    # ---- files -----------------------------------------------------------
    async def upload_file(self, path: str, content: bytes) -> None: ...

    async def download_file(self, path: str) -> bytes: ...

    # ---- commands ----------------------------------------------------------
    async def run(self, command: str, **kwargs: Any) -> Any: ...


__all__ = ["Sandbox"]

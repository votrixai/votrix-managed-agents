"""Adapt a Sandbox into the SandboxBackendProtocol DeepAgents expects.

Interface only. ``Sandbox`` (client.py) does not itself implement the
protocol create_deep_agent(backend=...) requires (.execute()/.ls()/.read()/
.write()/...). langchain_e2b.AsyncE2BSandbox already implements that
protocol and just needs a native e2b.AsyncSandbox to wrap.
"""

from __future__ import annotations

from langchain_e2b import AsyncE2BSandbox

from app.runtime.sandbox.client import Sandbox


def deep_agents_backend(sandbox: Sandbox) -> AsyncE2BSandbox:
    """Wrap ``sandbox.native`` so it can be passed as create_deep_agent(backend=...)."""
    ...


__all__ = ["deep_agents_backend"]

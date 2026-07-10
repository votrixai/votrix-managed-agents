from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeResult:
    final_text: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    requires_action: bool = False
    blocking_event_ids: list[str] = field(default_factory=list)
    events_persisted: bool = False
    run_state: dict[str, Any] | None = None
    sandbox_state: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class EffectiveAgentVersion:
    id: str
    agent_id: str
    version: int
    name: str
    model: dict[str, Any]
    system: str | None
    description: str | None
    tools: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    multiagent: dict[str, Any] | None
    metadata_: dict[str, Any]
    runtime: dict[str, Any]


# Returns the durable public event ID. Engines can omit the callback in unit tests
# and return events in RuntimeResult.tool_events instead.
RuntimeEventEmitter = Callable[[dict[str, Any]], Awaitable[str]]
RuntimePreviewEmitter = Callable[[dict[str, Any]], Awaitable[None]]

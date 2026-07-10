from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

DEFAULT_WORKSPACE_ID = "wrkspc_default"
DEFAULT_WORKSPACE_SLUG = "default"

_current_workspace: ContextVar["CurrentWorkspace | None"] = ContextVar(
    "current_workspace",
    default=None,
)


@dataclass(frozen=True)
class CurrentWorkspace:
    id: str
    slug: str = DEFAULT_WORKSPACE_SLUG
    source: str = "default"


def workspace_id_or_default(value: str | None = None) -> str:
    if value:
        return value
    workspace = _current_workspace.get()
    return workspace.id if workspace is not None else DEFAULT_WORKSPACE_ID


def default_workspace() -> CurrentWorkspace:
    return CurrentWorkspace(id=DEFAULT_WORKSPACE_ID)


def set_current_workspace(workspace: CurrentWorkspace) -> Token[CurrentWorkspace | None]:
    return _current_workspace.set(workspace)


def reset_current_workspace(token: Token[CurrentWorkspace | None]) -> None:
    _current_workspace.reset(token)


def current_workspace() -> CurrentWorkspace:
    return _current_workspace.get() or default_workspace()

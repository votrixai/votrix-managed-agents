"""The agent runtime: one turn, start to finish.

`execute_agent` is deliberately cut off from the database. It gets what it
needs as arguments and reports what it produced through `emit`; the session
service decides what that means and whether to keep writing. History is not
passed in either — that lives in the graph checkpoint, keyed by session id.

Everything the turn produces is a side effect of `emit`. There is nothing to
return, because every reader downstream goes to the event log instead.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from app.config import get_settings
from app.db.models import AgentVersion, Session
from app.models import events as event_types
from app.models.llm import ANTHROPIC, DEEPSEEK, GOOGLE, MODEL_CATALOG
from app.models.sessions import STOP_END_TURN, STOP_REQUIRES_ACTION
from app.runtime.tools import (
    AGENT_TOOLSET,
    WEB_TOOLSET,
    custom_tool,
    resolve_tool_interrupts,
    web_fetch_tool,
    web_search_tool,
)
from app.utils.sandbox import OUTPUTS_DIR, SKILLS_DIR, UPLOADS_DIR, WORKDIR, Sandbox

Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]


class UnsupportedEventError(RuntimeError):
    """An input event this cut of the engine does not know how to interpret."""


class FilesystemToolsetRequiredError(RuntimeError):
    """The agent declares no agent toolset, so it has nothing to work with."""


class UnknownModelError(ValueError):
    """A model id that is not in the catalogue."""


class MissingProviderKeyError(RuntimeError):
    """The model exists but this deployment has no key for its provider."""


async def execute_agent(
    *,
    session: Session,
    version: AgentVersion,
    message: dict[str, Any],
    sandbox: Sandbox,
    emit: Emit,
    attached_files: list[str] | None = None,
) -> None:
    """Run one turn to completion, or to the next point it needs the user.

    `message` is the single event that triggered this turn. Whether it starts a
    turn or answers a paused one is decided from its type, not by asking the
    checkpoint: DeepAgents' own PatchToolCallsMiddleware already handles a
    fresh message arriving while a tool call is still pending, cancelling the
    dangling call rather than erroring.

    `emit` refuses to write once the session has been interrupted, so a
    cancelled turn unwinds through the next call rather than running to the end.
    """
    config = {"configurable": {"thread_id": session.id}}
    declared = version.tools or []

    # DeepAgents' native tools are never filtered — whatever create_deep_agent()
    # installs is exactly what the model gets. Config only ever decides which of
    # them need a human decision first.
    if not any(isinstance(spec, dict) and spec.get("type") == AGENT_TOOLSET for spec in declared):
        raise FilesystemToolsetRequiredError(
            f"agent {version.agent_id} v{version.version} declares no {AGENT_TOOLSET}"
        )
    interrupt_on = resolve_tool_interrupts(declared)

    tools: list[Any] = []
    tool_kind: dict[str, str] = {}
    if any(isinstance(spec, dict) and spec.get("type") == WEB_TOOLSET for spec in declared):
        tools.append(web_fetch_tool())
        tools.append(web_search_tool())
    for spec in declared:
        if isinstance(spec, dict) and spec.get("type") == "custom":
            name = str(spec["name"])
            tools.append(custom_tool(spec))
            tool_kind[name] = "custom"
            # A custom tool has no implementation on our side at all. The only
            # way one ever completes is the client answering it.
            interrupt_on[name] = {"allowed_decisions": ["respond"]}
    # MCP tools (version.mcp_servers) are accepted and stored but not loaded.

    # DeepAgents scans this directory itself, reads each `<name>/SKILL.md`, and
    # puts the name and description into the system prompt — the full text only
    # gets read if the model decides the skill applies. Nothing for us to pass:
    # the packages were unpacked here when the sandbox was provisioned.
    skill_sources = [f"{SKILLS_DIR}/"] if version.skills else None

    async with _checkpoint_saver() as checkpointer:
        graph = create_deep_agent(
            model=_build_chat_model(version.model or {}),
            tools=tools,
            system_prompt=_system_prompt(version.system, attached_files or []),
            backend=sandbox.to_deep_agent_backend,
            skills=skill_sources,
            interrupt_on=interrupt_on or None,
            checkpointer=checkpointer,
        )

        await emit(event_types.SESSION_STATUS_RUNNING, {"status": "running"})

        state = await graph.aget_state(config)
        if event_types.is_action_result(message.get("type", "")):
            if not state.next:
                raise UnsupportedEventError(
                    "received an action result but the graph is not paused on an interrupt"
                )
            graph_input: Any = _build_resume(state, message, interrupt_on)
        else:
            graph_input = _build_fresh_input(message)

        # Only messages past this index are new. `open_tool_calls` tracks calls
        # that were announced but never answered; whatever is left when the
        # stream ends is exactly what the turn is now waiting on.
        last_index = len((state.values or {}).get("messages", []))
        open_tool_calls: set[str] = set()
        async for values in graph.astream(graph_input, config, stream_mode="values"):
            messages = values.get("messages", [])
            for msg in messages[last_index:]:
                await _translate(msg, emit, tool_kind, interrupt_on, open_tool_calls)
            last_index = len(messages)

        stop_reason = (
            {"type": STOP_REQUIRES_ACTION, "event_ids": sorted(open_tool_calls)}
            if open_tool_calls
            else {"type": STOP_END_TURN}
        )
        await emit(
            event_types.SESSION_STATUS_IDLE,
            {"status": "idle", "stop_reason": stop_reason},
        )


# --- turning the trigger event into graph input -----------------------------


def _build_fresh_input(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("type") != event_types.USER_MESSAGE:
        raise UnsupportedEventError(f"{message.get('type')} cannot start a turn")
    payload = message.get("payload") or {}
    return {"messages": [HumanMessage(content=_text(payload.get("content")))]}


def _build_resume(state: Any, message: dict[str, Any], interrupt_on: dict[str, Any]) -> Command:
    """The graph is paused: this turn answers what it is waiting on.

    HumanInTheLoopMiddleware bundles every tool call on the last AIMessage that
    needs a decision into one `interrupt()`, resumed with a single `decisions`
    list ordered the way it built the request — the order those calls appear in
    `tool_calls`. That order is rebuilt here from the checkpoint rather than
    read off the interrupt's own payload.
    """
    messages = (state.values or {}).get("messages", [])
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if last_ai is None or not last_ai.tool_calls:
        raise UnsupportedEventError("the graph is paused but has no pending tool calls")

    payload = message.get("payload") or {}
    answered = payload.get("tool_use_id") or payload.get("custom_tool_use_id")
    pending = [call["id"] for call in last_ai.tool_calls if call["name"] in interrupt_on]

    decisions = []
    for call_id in pending:
        if call_id != answered:
            raise UnsupportedEventError(f"no answer supplied for pending tool call {call_id}")
        decisions.append(_decision(message))
    return Command(resume={"decisions": decisions})


def _decision(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    if message["type"] == event_types.USER_TOOL_CONFIRMATION:
        if payload.get("result") == "allow":
            return {"type": "approve"}
        return {"type": "reject", "message": payload.get("deny_message") or ""}
    if message["type"] == event_types.USER_CUSTOM_TOOL_RESULT:
        return {"type": "respond", "message": _text(payload.get("content"))}
    # user.tool_result means the client ran an agent-toolset tool itself, which
    # does not apply while every tool runs in our sandbox.
    raise UnsupportedEventError(f"{message['type']} cannot answer an interrupt yet")


# --- turning graph output into events ----------------------------------------


async def _translate(
    message: Any,
    emit: Emit,
    tool_kind: dict[str, str],
    interrupt_on: dict[str, Any],
    open_tool_calls: set[str],
) -> None:
    if isinstance(message, AIMessage):
        text = _text(message.content)
        if text:
            await emit(event_types.AGENT_MESSAGE, {"content": [{"type": "text", "text": text}]})
        for call in message.tool_calls or []:
            is_custom = tool_kind.get(call["name"]) == "custom"
            await emit(
                event_types.AGENT_CUSTOM_TOOL_USE if is_custom else event_types.AGENT_TOOL_USE,
                {
                    "tool_use_id": call["id"],
                    "name": call["name"],
                    "input": call["args"],
                    "evaluated_permission": "ask" if call["name"] in interrupt_on else "allow",
                },
            )
            open_tool_calls.add(call["id"])
        return

    if isinstance(message, ToolMessage):
        open_tool_calls.discard(message.tool_call_id)
        await emit(
            event_types.AGENT_TOOL_RESULT,
            {
                "tool_use_id": message.tool_call_id,
                "content": [{"type": "text", "text": str(message.content)}],
                "is_error": message.status == "error",
            },
        )
        return

    # System/Human messages coming back off the checkpoint are the input we just
    # fed in, not new output.
    if isinstance(message, (SystemMessage, HumanMessage)):
        return


# --- pieces the graph needs --------------------------------------------------


@asynccontextmanager
async def _checkpoint_saver() -> AsyncIterator[Any]:
    """Where LangGraph keeps each session's graph state.

    This is the agent's memory of a conversation: the messages so far and, if
    it paused to ask something, exactly where it stopped. `session_events` is
    the record for clients; this is what the agent itself reads back.
    """
    url = str(get_settings().database_url)

    if url.startswith(("postgres://", "postgresql://", "postgresql+")):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(_postgres_dsn(url)) as saver:
            await saver.setup()
            yield saver
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    # A separate file, so LangGraph's migrations never mix with our schema.
    path = _sqlite_checkpoint_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        yield saver


def _system_prompt(configured: str | None, attached_files: list[str]) -> str | None:
    """Add the bit about the workspace that only we know.

    The agent is told where its inputs are and where to put its results,
    because neither is discoverable: `uploads/` is read-only and easy to miss,
    and nothing about `outputs/` says that what lands there is collected and
    handed back. Files are listed rather than left to an `ls`, which turns
    "find your input" into something the agent already knows.

    Nothing is added when nothing was attached, beyond where output goes.
    """
    lines = [
        "## Workspace",
        "",
        f"You are working in `{WORKDIR}`.",
        "",
        # Deliberately does not say the directory is collected afterwards.
        # Nothing collects it yet, and an agent told its work will be handed
        # back would stop mentioning results it believes the user already has.
        f"- `{OUTPUTS_DIR}` — put finished work here, so the user can tell "
        "deliverables from scratch files.",
    ]
    if attached_files:
        listed = "\n".join(f"  - `{UPLOADS_DIR}/{path}`" for path in attached_files)
        lines.append(
            f"- `{UPLOADS_DIR}` — files the user attached, read-only:\n{listed}"
        )
    else:
        lines.append(f"- `{UPLOADS_DIR}` — empty; the user attached no files.")

    workspace = "\n".join(lines)
    return f"{configured}\n\n{workspace}" if configured else workspace


def _build_chat_model(spec: dict[str, Any] | str) -> Any:
    """Keys come from configuration — a caller names a model, never a credential.

    One key per provider, and nothing falls back to another: a request for a
    model whose key is missing fails here rather than quietly running on
    something the caller did not ask for.
    """
    model_id = spec if isinstance(spec, str) else str(spec.get("id") or "")
    entry = next((m for m in MODEL_CATALOG if m.id == model_id), None)
    if entry is None:
        known = ", ".join(m.id for m in MODEL_CATALOG)
        raise UnknownModelError(f"Unknown model {model_id!r}. Known models: {known}")

    settings = get_settings()
    if entry.provider == ANTHROPIC:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model_id, api_key=_require_key(settings.anthropic_api_key, entry))
    if entry.provider == GOOGLE:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_id,
            google_api_key=_require_key(settings.gemini_api_key, entry),
        )
    if entry.provider == DEEPSEEK:
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(model=model_id, api_key=_require_key(settings.deepseek_api_key, entry))
    raise UnknownModelError(f"No client wired up for provider {entry.provider!r}")


def _require_key(key: str, entry: Any) -> str:
    if not key:
        raise MissingProviderKeyError(
            f"{entry.id} needs a {entry.provider} API key, which is not configured"
        )
    return key


def _postgres_dsn(value: str) -> str:
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    for driver in ("+asyncpg", "+psycopg", "+psycopg_async"):
        value = value.replace(driver, "")
    return value


def _sqlite_checkpoint_path(value: str) -> Path:
    if value.startswith(("sqlite+", "sqlite:")):
        raw = value.split("///", 1)[-1]
        path = Path("/" + raw) if ":////" in value else Path(raw)
        if path.name:
            return path.with_name(f"{path.stem}.checkpoints.sqlite3")
    return Path("./votrix_managed_agents.checkpoints.sqlite3")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


__all__ = ["FilesystemToolsetRequiredError", "UnsupportedEventError", "execute_agent"]

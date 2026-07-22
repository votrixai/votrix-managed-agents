"""Deep Agents execution engine, rebuilt on LangGraph's own checkpoint/interrupt primitives.

``execute_deep_agent`` is "one turn". A turn is triggered by a caller (a worker
picking up a queued session run) that already knows two things: which session,
and which events are new since the last turn. This function does not manage
retries, queueing, or cross-instance coordination — it assembles a graph, asks
the graph's own checkpoint where it left off, feeds the new events in the right
shape, and streams the result out as durable session events. Everything it
produces is a side effect (an appended event); it has nothing meaningful to
return, because every other part of the system (the events/stream APIs, a
future recovery attempt) only ever reads the event log, never this function's
return value.
"""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ManagedSession, SessionEvent
from app.db.queries.events import append_event
from app.db.queries.session_sandboxes import get_session_sandbox
from app.runtime.checkpoints import checkpoint_saver
from app.runtime.contracts import EffectiveAgentVersion
from app.runtime.deepagent_tools import (
    DEEP_TO_CLAUDE_TOOL,
    ToolFilterMiddleware,
    custom_tool,
    deep_tool_policy,
    web_fetch_tool,
    web_search_tool,
)
from app.runtime.providers import build_chat_model, resolve_runtime_provider
from app.runtime.sandbox import Sandbox


class UnsupportedEventError(RuntimeError):
    """An input event this cut of the engine does not yet know how to interpret."""


class SandboxRequiredError(RuntimeError):
    """A session reached execute_deep_agent with no provisioned sandbox."""


async def execute_deep_agent(
    db: AsyncSession,
    session: ManagedSession,
    version: EffectiveAgentVersion,
    new_events: list[SessionEvent],
    *,
    provider_secrets: dict[str, str] | None = None,
) -> None:
    """Run one turn to completion (or to the next pause point).

    new_events is whatever the caller decided triggered this turn — either a
    single user.message (plus an optional trailing system.message), or a batch
    of action-result events (user.tool_confirmation / user.custom_tool_result)
    answering everything a prior turn was waiting on. Which case this is gets
    decided below from the graph's own checkpoint, not from event type alone.
    """
    if not new_events:
        raise UnsupportedEventError("execute_deep_agent called with no new events")

    config = {"configurable": {"thread_id": session.id}}

    # Step 1: assemble this turn's tools, model, and sandbox backend. None of
    # this depends on new_events — it is pure agent/session configuration.
    #
    # A sandbox is a hard prerequisite, not an optional capability: sessions
    # are expected to already have one provisioned by the time they reach
    # execute_deep_agent. A missing sandbox means something upstream (session
    # setup) failed — silently degrading to a agent with no filesystem tools
    # would hide that failure, so this fails fast instead.
    sandbox_row = await get_session_sandbox(db, session.id, organization_id=session.organization_id)
    if sandbox_row is None:
        raise SandboxRequiredError(f"session {session.id} has no provisioned sandbox")
    sandbox = Sandbox.from_id(sandbox_row.external_sandbox_id, session.id, session.organization_id)
    backend = sandbox.to_deep_agent_backend

    # TODO: multiagent/subagents support is removed for now, not needed yet.
    # has_multiagent=False keeps the "task" (delegate-to-subagent) tool excluded.
    excluded, interrupt_on, _tool_config = deep_tool_policy(
        version.tools, supports_execute=True, has_multiagent=False
    )

    tool_kind: dict[str, str] = dict.fromkeys(DEEP_TO_CLAUDE_TOOL, "agent")
    tools: list[Any] = []
    if "web_fetch" not in excluded:
        tools.append(web_fetch_tool())
    if "web_search" not in excluded:
        tools.append(web_search_tool())
    for spec in version.tools:
        if isinstance(spec, dict) and spec.get("type") == "custom":
            name = str(spec["name"])
            tools.append(custom_tool(spec))
            tool_kind[name] = "custom"
            # Custom tools have no server-side implementation at all — the only
            # way one ever completes is a human/client "respond" decision.
            interrupt_on[name] = {"allowed_decisions": ["respond"]}
    # TODO: MCP tools (version.mcp_servers) are not loaded yet. Wiring them in
    # means adding their names to `tools`, `tool_kind` (as "mcp"), and, for any
    # flagged always_ask, to `interrupt_on` — same shape as the custom-tool loop
    # above.

    provider_config = resolve_runtime_provider(version.model, runtime=version.runtime, secrets=provider_secrets)
    model = build_chat_model(provider_config)

    async with checkpoint_saver() as checkpointer:
        graph = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=version.system,
            middleware=[ToolFilterMiddleware(excluded=excluded)] if excluded else [],
            backend=backend,
            interrupt_on=interrupt_on or None,
            checkpointer=checkpointer,
        )

        # Step 2: this turn is really about to run — announce it.
        await _emit(db, session, "session.status_running", {"status": "running"})

        # Step 3: ask the checkpoint where this session's graph currently sits.
        # Empty `next` means the graph is at rest (last turn ended naturally);
        # non-empty means it is paused mid-interrupt, waiting on exactly the
        # kind of event that triggered this turn.
        state = await graph.aget_state(config)

        # Step 4: turn new_events into the right shape for this graph.
        if state.next:
            graph_input: Any = _build_resume(state, new_events, interrupt_on)
        else:
            graph_input = _build_fresh_input(new_events)

        # Step 5: stream the graph; translate every newly-appended message into
        # a durable event. `open_tool_calls` tracks tool_call ids that got an
        # agent.tool_use/agent.mcp_tool_use/agent.custom_tool_use event but no
        # matching ToolMessage yet — whatever is left in it once the stream
        # ends is exactly what this turn is now waiting on.
        last_index = len((state.values or {}).get("messages", []))
        open_tool_calls: set[str] = set()
        async for values in graph.astream(graph_input, config, stream_mode="values"):
            messages = values.get("messages", [])
            for message in messages[last_index:]:
                await _translate_message(db, session, message, tool_kind, interrupt_on, open_tool_calls)
            last_index = len(messages)

        # Step 6: this turn is over — record why it stopped.
        if open_tool_calls:
            await _emit(
                db,
                session,
                "session.status_idle",
                {
                    "status": "idle",
                    "stop_reason": {"type": "requires_action", "event_ids": sorted(open_tool_calls)},
                },
            )
        else:
            await _emit(
                db,
                session,
                "session.status_idle",
                {"status": "idle", "stop_reason": {"type": "end_turn"}},
            )


async def _emit(
    db: AsyncSession,
    session: ManagedSession,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> SessionEvent:
    body = dict(payload)
    body.setdefault("type", event_type)
    return await append_event(db, session, event_type=event_type, payload=body, event_id=event_id)


def _build_fresh_input(new_events: list[SessionEvent]) -> dict[str, Any]:
    """The graph is at rest: this turn must be a plain new user message."""
    user_events = [event for event in new_events if event.type == "user.message"]
    if len(user_events) != 1:
        raise UnsupportedEventError(
            f"graph is at rest, expected exactly one user.message, got {len(user_events)}"
        )
    system_events = [event for event in new_events if event.type == "system.message"]

    messages: list[Any] = [
        SystemMessage(content=_text_from_blocks(event.payload.get("content"))) for event in system_events
    ]
    messages.append(HumanMessage(content=_text_from_blocks(user_events[0].payload.get("content"))))
    return {"messages": messages}


def _build_resume(
    state: Any,
    new_events: list[SessionEvent],
    interrupt_on: dict[str, Any],
) -> Command:
    """The graph is paused: this turn must be answers to what it's waiting on.

    HumanInTheLoopMiddleware bundles every tool call on the last AIMessage that
    needs a decision into a single `interrupt()`, resumed with one
    `{"decisions": [...]}` value ordered the same way it built the request: the
    order those tool calls appear in `last_ai_msg.tool_calls`. We rebuild that
    same order here from the checkpoint instead of inspecting the interrupt's
    own payload, so this only needs the interrupt_on config already computed
    above (Step 1) and the session's own event log.
    """
    messages = (state.values or {}).get("messages", [])
    last_ai = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
    if last_ai is None or not last_ai.tool_calls:
        raise UnsupportedEventError("graph state.next is non-empty but no pending tool_calls were found")

    pending_call_ids = [call["id"] for call in last_ai.tool_calls if call["name"] in interrupt_on]
    answers = {
        event.payload.get("tool_use_id") or event.payload.get("custom_tool_use_id"): event for event in new_events
    }

    decisions = []
    for call_id in pending_call_ids:
        event = answers.get(call_id)
        if event is None:
            raise UnsupportedEventError(f"no answer event found for pending tool call {call_id}")
        decisions.append(_decision_from_event(event))
    return Command(resume={"decisions": decisions})


def _decision_from_event(event: SessionEvent) -> dict[str, Any]:
    if event.type == "user.tool_confirmation":
        if event.payload.get("result") == "allow":
            return {"type": "approve"}
        return {"type": "reject", "message": event.payload.get("deny_message") or ""}
    if event.type == "user.custom_tool_result":
        return {"type": "respond", "message": _text_from_blocks(event.payload.get("content"))}
    # user.tool_result (self_hosted: client executes agent-toolset tools itself)
    # has no backend in VMA's sandbox-based model yet.
    raise UnsupportedEventError(f"{event.type} is not supported as a resume answer yet")


async def _translate_message(
    db: AsyncSession,
    session: ManagedSession,
    message: Any,
    tool_kind: dict[str, str],
    interrupt_on: dict[str, Any],
    open_tool_calls: set[str],
) -> None:
    if isinstance(message, AIMessage):
        text = _text_from_content(message.content)
        if text:
            await _emit(db, session, "agent.message", {"content": [{"type": "text", "text": text}]})
        for call in message.tool_calls or []:
            event_type = {
                "agent": "agent.tool_use",
                "mcp": "agent.mcp_tool_use",
                "custom": "agent.custom_tool_use",
            }[tool_kind.get(call["name"], "agent")]
            permission = "ask" if call["name"] in interrupt_on else "allow"
            await _emit(
                db,
                session,
                event_type,
                {"name": call["name"], "input": call["args"], "evaluated_permission": permission},
                event_id=call["id"],
            )
            open_tool_calls.add(call["id"])
        return

    if isinstance(message, ToolMessage):
        open_tool_calls.discard(message.tool_call_id)
        await _emit(
            db,
            session,
            "agent.tool_result",
            {
                "tool_use_id": message.tool_call_id,
                "content": [{"type": "text", "text": str(message.content)}],
                "is_error": message.status == "error",
            },
        )
        return

    # SystemMessage/HumanMessage echoes from the checkpoint's own message list
    # are the input we just fed in, not new output — nothing to emit for them.


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text"
    )


def _text_from_blocks(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("type") == "text"
    )


__all__ = ["SandboxRequiredError", "UnsupportedEventError", "execute_deep_agent"]

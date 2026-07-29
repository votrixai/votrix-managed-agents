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
from app.db.queries.sessions import update_session
from app.runtime.checkpoints import checkpoint_saver
from app.runtime.contracts import EffectiveAgentVersion
from app.runtime.deepagent_tools import custom_tool, resolve_tool_interrupts, web_fetch_tool, web_search_tool
from app.runtime.providers import build_chat_model, resolve_runtime_provider
from app.runtime.sandbox import Sandbox
from app.session_state import SESSION_IDLE, is_action_result


class UnsupportedEventError(RuntimeError):
    """An input event this cut of the engine does not yet know how to interpret."""


class SandboxRequiredError(RuntimeError):
    """A session reached execute_deep_agent with no provisioned sandbox."""


class FilesystemToolsetRequiredError(RuntimeError):
    """A session's agent has no agent_toolset_20260401 toolset declared."""


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
    decided from the event types themselves (see app.session_state), not by
    asking the checkpoint whether it's paused: deepagents' own
    PatchToolCallsMiddleware already handles the (should-not-happen-in-practice)
    case of a fresh message arriving while something is still pending — it
    gracefully cancels the dangling tool call rather than erroring.
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

    # deepagents' own native tools (write_todos, the filesystem tools, execute,
    # task/subagent-spawning) are never filtered: whatever create_deep_agent()
    # installs by default is exactly what the model gets. Toolset config only
    # ever controls whether a given tool needs a human approve/reject decision
    # before it runs — never whether the tool exists at all.
    if not any(isinstance(spec, dict) and spec.get("type") == "agent_toolset_20260401" for spec in version.tools):
        raise FilesystemToolsetRequiredError(f"session {session.id}'s agent has no agent_toolset_20260401 toolset")
    interrupt_on = resolve_tool_interrupts(version.tools)

    has_web_toolset = any(
        isinstance(spec, dict) and spec.get("type") == "web_toolset_20260401" for spec in version.tools
    )
    tool_kind: dict[str, str] = {}
    tools: list[Any] = []
    if has_web_toolset:
        tools.append(web_fetch_tool())
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
            backend=backend,
            interrupt_on=interrupt_on or None,
            checkpointer=checkpointer,
        )

        # Step 2: this turn is really about to run — announce it.
        await _emit(db, session, "session.status_running", {"status": "running"})

        # Step 3: fetch the checkpoint's current message history. Needed for
        # Step 5's "which messages are new" diff, and — when this turn is a
        # resume — for _build_resume's lookup of the last AIMessage.tool_calls.
        state = await graph.aget_state(config)

        # Step 4: turn new_events into the right shape for this graph. An
        # action-result event always means answering a pending decision; any
        # other event always goes in as fresh input. If new_events claims to
        # be answering something but the graph isn't actually paused, that is
        # a caller bug (session_state.py's can_start_work should have already
        # prevented this) — fail loudly instead of silently misinterpreting it.
        if any(is_action_result(event.type) for event in new_events):
            if not state.next:
                raise UnsupportedEventError(
                    "received action-result events but the graph is not paused on an interrupt"
                )
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

        # Step 6: this turn is over — record why it stopped, and persist that
        # onto the session row itself (the event above is what clients see;
        # this is what the next turn's session_state checks actually read).
        if open_tool_calls:
            stop_reason = {"type": "requires_action", "event_ids": sorted(open_tool_calls)}
        else:
            stop_reason = {"type": "end_turn"}
        await update_session(db, session, status=SESSION_IDLE, stop_reason=stop_reason)
        await _emit(db, session, "session.status_idle", {"status": "idle", "stop_reason": stop_reason})


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


__all__ = [
    "FilesystemToolsetRequiredError",
    "SandboxRequiredError",
    "UnsupportedEventError",
    "execute_deep_agent",
]

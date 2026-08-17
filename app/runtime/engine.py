"""The agent runtime: one turn, start to finish.

`execute_agent` is deliberately cut off from the database. It gets what it
needs as arguments and reports what it produced through `emit`; the session
service decides what that means and whether to keep writing. History is not
passed in either — that lives in the graph checkpoint, keyed by session id.

Everything the turn produces is a side effect of `emit`. There is nothing to
return, because every reader downstream goes to the event log instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from app.config import get_settings
from app.db.models import AgentVersion, Session
from app.models import events as event_types
from app.models.llm import DEEPSEEK, MODEL_CATALOG, OPENROUTER_SLUGS
from app.models.sessions import STOP_END_TURN, STOP_REQUIRES_ACTION
from app.runtime.tools import (
    AGENT_TOOLSET,
    WEB_TOOLSET,
    custom_tool,
    read_image_tool,
    resolve_tool_interrupts,
    web_fetch_tool,
    web_search_tool,
)
from app.utils.sandbox import OUTPUTS_DIR, SKILLS_DIR, UPLOADS_DIR, WORKDIR, Sandbox
from app.utils.timing import timed

Emit = Callable[[str, dict[str, Any]], Awaitable[Any]]
ToolCompleted = Callable[[str], Awaitable[None]]
_FILESYSTEM_MUTATORS = {"write_file", "edit_file", "execute"}

# No `task` tool, and so no subagents. DeepAgents adds a `general-purpose`
# subagent unless told otherwise, and only attaches `SubAgentMiddleware` — the
# thing that carries `task` — when at least one synchronous subagent exists.
# We pass no `subagents=`, so disabling the default one leaves the middleware
# out entirely rather than leaving a tool nobody can reach.
#
# Why it is off: that subagent inherits the parent's tools *and* its skills,
# but not its system prompt and not the conversation. A copy of an agent that
# has been told how to talk to a user, has every paid tool, and knows nothing
# about what the user asked, will hold a conversation with its own parent —
# and spend money doing it. Worse, none of that reaches the client: the
# subagent's calls are internal to the graph, so the whole run appears in the
# event log as a single `task` with no cost and no trace attached.
#
# Registered under the provider rather than a model id because every model in
# the catalogue is reached through `ChatOpenRouter` (see `_build_chat_model`),
# which resolves to provider `openrouter`. Profile lookup falls back from
# `provider:identifier` to the bare provider, so this one entry covers the
# whole catalogue. There is no wildcard key; a second gateway would need its
# own registration here.
register_harness_profile(
    "openrouter",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


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
    events: list[dict[str, Any]],
    sandbox: Sandbox,
    emit: Emit,
    inference_key: str,
    attached_files: list[str] | None = None,
    attached_memory_stores: list[dict[str, Any]] | None = None,
    tool_completed: ToolCompleted | None = None,
) -> dict[str, Any]:
    """Run one turn to completion, or to the next point it needs the user.

    Returns why it stopped, in the shape the `session.status_idle` event
    carries it.

    `events` is the batch that triggered this turn. Whether it starts a turn or
    answers a paused one is decided from the first event's type, not by asking
    the checkpoint: DeepAgents' own PatchToolCallsMiddleware already handles a
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
    # Part of the filesystem toolset rather than the web one: it opens a file in
    # the same container `read_file` reads from, and it is here because
    # `read_file` hands an image back as a content block — which is an answer
    # only if the agent's own model has eyes.
    tools.append(read_image_tool(sandbox, api_key=inference_key))
    if any(isinstance(spec, dict) and spec.get("type") == WEB_TOOLSET for spec in declared):
        tools.append(web_search_tool())
        tools.append(web_fetch_tool(sandbox))

    for spec in declared:
        if isinstance(spec, dict) and spec.get("type") == "custom":
            name = str(spec["name"])
            tools.append(custom_tool(spec))
            tool_kind[name] = "custom"
            # A custom tool has no implementation on our side at all. The only
            # way one ever completes is the client answering it — with the
            # result, or with the news that it failed. Those are two different
            # decisions to the middleware: `respond` synthesises a successful
            # ToolMessage and `reject` an errored one, and the model has to be
            # able to tell which it got.
            interrupt_on[name] = {"allowed_decisions": ["respond", "reject"]}
    # MCP tools (version.mcp_servers) are accepted and stored but not loaded.

    # DeepAgents scans this directory itself, reads each `<name>/SKILL.md`, and
    # puts the name and description into the system prompt — the full text only
    # gets read if the model decides the skill applies. Nothing for us to pass:
    # the packages were unpacked here when the sandbox was provisioned.
    skill_sources = [f"{SKILLS_DIR}/"] if version.skills else None

    async with _checkpoint_saver(session.id) as checkpointer:
        async with timed(
            "graph_built", session_id=session.id, tools=len(tools), skills=bool(skill_sources)
        ):
            graph = create_deep_agent(
                # The session's own model wins when it named one; otherwise the
                # agent version's applies. Resolved here on every turn rather
                # than copied onto the session at creation, so a session that
                # expressed no preference keeps following the agent instead of
                # a frozen snapshot of it.
                #
                # Which model to run and whose credential pays for it are
                # separate questions: the model comes off the session or the
                # agent, the key off the session's Account.
                model=_build_chat_model(
                    session.model or version.model or {}, api_key=inference_key
                ),
                tools=tools,
                system_prompt=_system_prompt(
                    version.system,
                    attached_files or [],
                    attached_memory_stores or [],
                ),
                backend=sandbox.to_deep_agent_backend,
                skills=skill_sources,
                # Whether a call stops for a human is decided by tool name, and
                # `interrupt_on` is the whole of it. Do not pass `permissions`:
                # DeepAgents turns those into per-call predicates and merges
                # them in here, and from that moment the `evaluated_permission`
                # we already told the client is a guess about arguments we
                # never saw.
                interrupt_on=interrupt_on or None,
                checkpointer=checkpointer,
            )

        await emit(event_types.SESSION_STATUS_RUNNING, {})

        state = await graph.aget_state(config)
        if event_types.is_action_result(events[0].get("type", "")):
            if not state.next:
                raise UnsupportedEventError(
                    "received an action result but the graph is not paused on an interrupt"
                )
            graph_input: Any = _build_resume(state, events, interrupt_on)
        else:
            graph_input = _build_fresh_input(events)

        # Only messages past this index are new. `announced` stops one call from
        # being reported twice: the middleware rewrites the last `AIMessage` in
        # place when it applies decisions, so the same message comes past again.
        last_index = len((state.values or {}).get("messages", []))
        announced: set[str] = set()
        # The whole of the turn's real work — the model, its tools, and every
        # checkpoint write between the nodes — happens inside this loop, so
        # this number minus everything reported from within it is what is
        # still unaccounted for.
        async with timed("graph_streamed", session_id=session.id) as span:
            steps = 0
            async for values in graph.astream(graph_input, config, stream_mode="values"):
                steps += 1
                messages = values.get("messages", [])
                for msg in messages[last_index:]:
                    await _translate(
                        msg,
                        emit,
                        tool_kind,
                        interrupt_on,
                        announced,
                        tool_completed,
                    )
                last_index = len(messages)
            span["steps"] = steps
            span["messages"] = last_index

        # Read the graph again rather than infer: whether it stopped for a human
        # is a fact it records, and deciding for ourselves would be a second
        # opinion that can disagree with it.
        stop_reason = _stop_reason(await graph.aget_state(config), interrupt_on)
        await emit(event_types.SESSION_STATUS_IDLE, {"stop_reason": stop_reason})
        # Handed back as well as emitted. The event tells the client why the
        # turn ended; the caller puts the same answer on the session row, where
        # the gate can see it — a turn that stopped to ask leaves the session
        # idle, and only the row says it is not free.
        return stop_reason


# --- turning the trigger event into graph input -----------------------------


def _build_fresh_input(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Messages, in the order the client sent them.

    A batch of several is fine — the graph takes a list — and each one is a
    turn's worth of input the model reads together.
    """
    messages = []
    for event in events:
        if event.get("type") != event_types.USER_MESSAGE:
            raise UnsupportedEventError(f"{event.get('type')} cannot start a turn")
        messages.append(HumanMessage(content=_text(event.get("content"))))
    return {"messages": messages}


def _pending_calls(state: Any, interrupt_on: dict[str, Any]) -> list[str]:
    """The calls the paused graph is waiting on, in the order it wants answers.

    `HumanInTheLoopMiddleware` builds its request by walking the last
    `AIMessage`'s `tool_calls` and keeping the ones named in `interrupt_on`.
    Walking it the same way here gives the same list in the same order — and
    with the ids attached, which the interrupt's own payload does not carry.
    """
    messages = (state.values or {}).get("messages", [])
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if last_ai is None or not last_ai.tool_calls:
        raise UnsupportedEventError("the graph is paused but has no pending tool calls")
    return [call["id"] for call in last_ai.tool_calls if call["name"] in interrupt_on]


def _build_resume(
    state: Any, events: list[dict[str, Any]], interrupt_on: dict[str, Any]
) -> Command:
    """The graph is paused: this turn answers what it is waiting on.

    The middleware matches decisions to its request **by position**, so the
    whole job here is putting the client's answers in the right order. Each one
    names the call it belongs to, which means a client may send them in any
    order at all — and it costs nothing to accept that, while getting the order
    wrong would apply a refusal to a call the user meant to allow, with nothing
    anywhere to show it happened.

    An incomplete set is passed on as it is rather than refused here. The
    middleware counts what it receives and raises, before any tool runs, with
    the same effect and one fewer place to keep the rule.
    """
    answers = {
        event.get("tool_use_id") or event.get("custom_tool_use_id"): event for event in events
    }
    decisions = [
        _decision(answers[call_id])
        for call_id in _pending_calls(state, interrupt_on)
        if call_id in answers
    ]
    return Command(resume={"decisions": decisions})


def _decision(event: dict[str, Any]) -> dict[str, Any]:
    if event["type"] == event_types.USER_TOOL_CONFIRMATION:
        if event.get("result") == "allow":
            return {"type": "approve"}
        return {"type": "reject", "message": event.get("deny_message") or ""}
    if event["type"] == event_types.USER_CUSTOM_TOOL_RESULT:
        # `respond` is documented as "the human answered on behalf of the
        # tool", and it lands as a *successful* ToolMessage. A client whose
        # tool failed has to reach the model as a failure, or the model reads
        # the error text as data and carries on with it.
        decision = "reject" if event.get("is_error") else "respond"
        return {"type": decision, "message": _text(event.get("content"))}
    # user.tool_result means the client ran an agent-toolset tool itself, which
    # does not apply while every tool runs in our sandbox.
    raise UnsupportedEventError(f"{event['type']} cannot answer an interrupt yet")


def _stop_reason(state: Any, interrupt_on: dict[str, Any]) -> dict[str, Any]:
    """Why the turn ended, taken from the graph rather than inferred.

    Whether it stopped for a human is something the graph records. Counting
    tool calls that have no result yet would answer the same question from our
    side of the glass, and could disagree.
    """
    if not (getattr(state, "interrupts", ()) or ()):
        return {"type": STOP_END_TURN}
    return {
        "type": STOP_REQUIRES_ACTION,
        "tool_use_ids": _pending_calls(state, interrupt_on),
    }


# --- turning graph output into events ----------------------------------------


async def _translate(
    message: Any,
    emit: Emit,
    tool_kind: dict[str, str],
    interrupt_on: dict[str, Any],
    announced: set[str],
    tool_completed: ToolCompleted | None = None,
) -> None:
    if isinstance(message, AIMessage):
        # `content_blocks` is LangChain's normalised view: every provider's own
        # way of marking reasoning arrives here as one block type, so thinking
        # and speech separate without knowing which model produced them.
        for block in message.content_blocks or []:
            kind = block.get("type")
            if kind == "reasoning" and block.get("reasoning"):
                await emit(
                    event_types.AGENT_THINKING,
                    {"content": [{"type": "text", "text": block["reasoning"]}]},
                )
            elif kind == "text" and block.get("text"):
                await emit(
                    event_types.AGENT_MESSAGE,
                    {"content": [{"type": "text", "text": block["text"]}]},
                )
        for call in message.tool_calls or []:
            # The middleware rewrites the last `AIMessage` in place when it
            # applies decisions, so the same message can come past us twice.
            if call["id"] in announced:
                continue
            announced.add(call["id"])
            is_custom = tool_kind.get(call["name"]) == "custom"
            payload: dict[str, Any] = {
                # The engine's own id for this call. It comes back unchanged on
                # the result, and unchanged again from the client answering it,
                # which is the whole of how the three are tied together.
                "tool_use_id": call["id"],
                "name": call["name"],
                "input": call["args"],
            }
            if not is_custom:
                payload["evaluated_permission"] = (
                    "ask" if call["name"] in interrupt_on else "allow"
                )
            await emit(
                event_types.AGENT_CUSTOM_TOOL_USE if is_custom else event_types.AGENT_TOOL_USE,
                payload,
            )
        return

    if isinstance(message, ToolMessage):
        await emit(
            event_types.AGENT_TOOL_RESULT,
            {
                "tool_use_id": message.tool_call_id,
                "content": [{"type": "text", "text": str(message.content)}],
                "is_error": message.status == "error",
            },
        )
        tool_name = str(message.name or "")
        if (
            tool_completed is not None
            and message.status != "error"
            and tool_name in _FILESYSTEM_MUTATORS
        ):
            await tool_completed(tool_name)
        return

    # System/Human messages coming back off the checkpoint are the input we just
    # fed in, not new output.
    if isinstance(message, (SystemMessage, HumanMessage)):
        return


# --- pieces the graph needs --------------------------------------------------


# The checkpointer's own calls, timed. LangGraph writes state between every
# pair of nodes, so a seven-node graph does this many times per turn — and it
# does it on its own psycopg connection, which means none of it appears in the
# statement timings installed on our SQLAlchemy engine. Without these probes
# the busiest database client in the process is the invisible one.
_CHECKPOINT_CALLS = ("aget_tuple", "aput", "aput_writes")

# `setup()` is LangGraph's migration check. It is idempotent and, after the
# first run, has nothing to do — but it is still a round trip to the database
# on every turn, and turns are the thing this service does. Once per process
# per database, guarded by a lock so two turns starting together do not both
# decide they are the first.
_setup_done: set[str] = set()
_setup_lock = asyncio.Lock()


async def _setup_once(saver: Any, key: str) -> None:
    if key in _setup_done:
        return
    async with _setup_lock:
        if key in _setup_done:
            return
        await saver.setup()
        _setup_done.add(key)


# One checkpoint pool for the process, keyed by DSN so a changed setting cannot
# hand back a pool pointing somewhere else. A turn used to open its own
# connection and close it after — a full TCP, TLS and SCRAM handshake against
# the hosted pooler, measured at 2–3 seconds, paid before the agent could read
# a single message back.
_checkpoint_pools: dict[str, Any] = {}
_checkpoint_pool_lock = asyncio.Lock()


async def _checkpoint_pool(dsn: str) -> Any:
    """The pool LangGraph's saver borrows from, opened once per process.

    `prepare_threshold=None` is the load-bearing argument, and the reason this
    does not use `AsyncPostgresSaver.from_conn_string`: that helper hardcodes
    `prepare_threshold=0`, meaning "prepare every statement immediately".
    Server-side prepared statements belong to one backend connection, so an
    agent that prepares on one and executes on another — which is what a
    transaction pooler does between transactions — fails with "prepared
    statement does not exist". Disabling them is what lets checkpoint traffic
    share Postgres backends instead of pinning one per running turn.

    `autocommit` and `dict_row` match what the helper sets; only the threshold
    deliberately differs.
    """

    if (pool := _checkpoint_pools.get(dsn)) is not None:
        return pool

    async with _checkpoint_pool_lock:
        if (pool := _checkpoint_pools.get(dsn)) is not None:
            return pool

        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        settings = get_settings()
        pool = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=settings.vma_checkpoint_pool_max_size,
            max_lifetime=settings.vma_checkpoint_pool_max_lifetime_seconds,
            # Checked on checkout, like the SQLAlchemy engine's pre-ping and for
            # the reason recorded there: the pooler in front of this database
            # drops idle connections well inside any recycle window, and age
            # alone was tried and produced thirty `connection is closed`
            # failures in one live run. A connection handed out dead is a failed
            # turn; a connection tested first is a slower one.
            check=AsyncConnectionPool.check_connection,
            open=False,
            configure=_configure_checkpoint_connection,
            kwargs={
                "autocommit": True,
                "prepare_threshold": None,
                "row_factory": dict_row,
            },
        )
        await pool.open()
        _checkpoint_pools[dsn] = pool
        return pool


async def aclose_checkpoint_pools() -> None:
    """Close every checkpoint pool. Called from the application lifespan."""

    async with _checkpoint_pool_lock:
        pools = list(_checkpoint_pools.values())
        _checkpoint_pools.clear()
    for pool in pools:
        await pool.close()


async def _configure_checkpoint_connection(conn: Any) -> None:
    """Point a freshly-opened checkpoint connection at the configured schema.

    Runs per connection, from the pool's `configure` hook, because Supabase's
    pooler drops PostgreSQL startup ``options`` — the schema cannot ride in on
    the DSN.

    **This is durable only on a session-mode DSN.** Through the transaction
    pooler the setting lands on whichever backend served this statement and is
    gone by the next one, so a hosted deployment that puts checkpoints on
    ``:6543`` must set the search path on the role instead:

        ALTER ROLE <db user> SET search_path = <schema>, public;

    A role default is applied by PostgreSQL to every backend at connect time,
    which is the only place a pooled client cannot lose it.
    """

    schema = get_settings().database_schema
    if not schema:
        return
    await conn.execute("SELECT set_config('search_path', %s, false)", (schema,))
    # Verified, and the pool raises out of `configure` if it did not take. The
    # silent failure is the dangerous one: LangGraph would go on to create and
    # read its tables in `public`, and a turn would resume from a checkpoint
    # history that simply is not there.
    cursor = await conn.execute("SELECT current_schema() AS current_schema")
    row = await cursor.fetchone()
    current_schema = (
        row.get("current_schema")
        if isinstance(row, Mapping)
        else row[0] if row else None
    )
    if current_schema != schema:
        raise RuntimeError(
            "Checkpoint connection did not select the configured database schema: "
            f"expected {schema!r}, got {current_schema!r}"
        )


def _instrument_saver(saver: Any, session_id: str) -> Any:
    """Time the checkpointer's calls by shadowing them on the instance.

    Set on the object rather than the class: two sessions can be running in
    one process, and each needs its own session id on the log line.
    """
    for name in _CHECKPOINT_CALLS:
        original = getattr(saver, name, None)
        if original is None:
            continue
        setattr(saver, name, _timed_call(name, original, session_id))
    return saver


def _timed_call(name: str, original: Any, session_id: str) -> Any:
    async def _call(*args: Any, **kwargs: Any) -> Any:
        async with timed("checkpoint_call", session_id=session_id, call=name):
            return await original(*args, **kwargs)

    return _call


@asynccontextmanager
async def _checkpoint_saver(session_id: str) -> AsyncIterator[Any]:
    """Where LangGraph keeps each session's graph state.

    This is the agent's memory of a conversation: the messages so far and, if
    it paused to ask something, exactly where it stopped. `session_events` is
    the record for clients; this is what the agent itself reads back.

    Opening it is timed separately from `setup()`. They fail differently and
    cost differently: the first is a fresh connection to Postgres, the second
    is a migration check that runs on every turn and almost never has anything
    to do.
    """
    settings = get_settings()
    url = (
        settings.vma_checkpoint_database_url.strip()
        or str(settings.database_url)
    )
    schema = settings.database_schema

    if url.startswith(("postgres://", "postgresql://", "postgresql+")):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with timed("checkpoint_connected", session_id=session_id, backend="postgres"):
            pool = await _checkpoint_pool(_postgres_dsn(url, schema))
        saver = AsyncPostgresSaver(conn=pool)
        # Force the explicit-transaction path. LangGraph batches a checkpoint's
        # writes into a psycopg pipeline, which is a substitute for a
        # transaction, not a transaction: through a transaction pooler the
        # statements in one batch can land on different backends, and a
        # checkpoint whose blobs were written but whose row was not reads back
        # as a complete state that is missing half of itself. The library's own
        # fallback for "no pipeline support" is `conn.transaction()`, which is
        # exactly what keeps a pooled client on one backend.
        saver.supports_pipeline = False
        async with timed("checkpoint_setup", session_id=session_id, backend="postgres"):
            await _setup_once(saver, f"{url}#{schema}")
        yield _instrument_saver(saver, session_id)
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    # A separate file, so LangGraph's migrations never mix with our schema.
    path = _sqlite_checkpoint_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        async with timed("checkpoint_setup", session_id=session_id, backend="sqlite"):
            # Keyed by the file, not the URL: a SQLite checkpoint lives in its
            # own file beside the database, and each one is set up once.
            await _setup_once(saver, str(path))
        yield _instrument_saver(saver, session_id)


def _system_prompt(
    configured: str | None,
    attached_files: list[str],
    attached_memory_stores: list[dict[str, Any]] | None = None,
) -> str | None:
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

    if attached_memory_stores:
        lines.extend(
            [
                "",
                "## Memory Stores",
                "",
                "These directories persist across Sessions. Only writes below an exact "
                "mount path update that Memory Store.",
            ]
        )
        for store in attached_memory_stores:
            lines.extend(
                [
                    "",
                    f"- **{store['name']}**",
                    f"  - Path: `{store['mount_path']}`",
                    f"  - Access: `{store['access']}`",
                    f"  - Description: {store.get('description') or '(none)'}",
                ]
            )
            if store.get("instructions"):
                lines.append(f"  - Instructions: {store['instructions']}")

    workspace = "\n".join(lines)
    return f"{configured}\n\n{workspace}" if configured else workspace


def _build_chat_model(spec: dict[str, Any] | str, *, api_key: str) -> Any:
    """A caller names a model; the credential is handed in, never looked up.

    Every model is reached through one gateway. The catalog's `provider`
    survives only as a label for a picker: it no longer selects a client, so
    adding a model is a catalog entry and its slug, never a new dependency.

    The key belongs to the Account paying for this turn, so it arrives as an
    argument. Reading it from configuration here would mean this function
    decides who pays, which is a question about the Session it cannot see.
    """
    model_id = spec if isinstance(spec, str) else str(spec.get("id") or "")
    entry = next((m for m in MODEL_CATALOG if m.id == model_id), None)
    if entry is None:
        known = ", ".join(m.id for m in MODEL_CATALOG)
        raise UnknownModelError(f"Unknown model {model_id!r}. Known models: {known}")

    slug = OPENROUTER_SLUGS.get(entry.id)
    if slug is None:
        # A catalog entry with no slug is a packaging mistake, not a bad
        # request: the caller named a model this build claims to serve.
        raise UnknownModelError(f"Model {entry.id!r} has no gateway slug configured")

    from langchain_openrouter import ChatOpenRouter

    provider_routing = (
        {"order": [DEEPSEEK], "allow_fallbacks": False}
        if entry.provider == DEEPSEEK
        else None
    )
    return ChatOpenRouter(
        model=slug,
        api_key=_require_key(api_key),
        openrouter_provider=provider_routing,
    )


def _require_key(key: str) -> str:
    if not key:
        raise MissingProviderKeyError(
            "no gateway credential was resolved for this turn, so no model can be reached"
        )
    return key


def _postgres_dsn(value: str, schema: str = "") -> str:
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    for driver in ("+asyncpg", "+psycopg", "+psycopg_async"):
        value = value.replace(driver, "")
    if schema:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(value)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={schema}"
        value = urlunsplit(parts._replace(query=urlencode(query)))
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

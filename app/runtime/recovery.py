"""Checkpoint recovery for an owned turn; public events keep their usual shape."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

if TYPE_CHECKING:
    from app.services.turn_execution import Execution


class RecoveryRequired(RuntimeError):
    """An interrupted tool may have had a side effect; require user intervention."""


class Recovery:
    def __init__(self, execution: "Execution") -> None:
        self.execution = execution
        self.seen = set()

    def matches(self, state: Any) -> bool:
        return (state.metadata or {}).get("vma_turn_id") == self.execution.key

    async def prepare(self, state: Any) -> None:
        if self.execution.history_ids is None:
            await self.execution.save_history(
                [m.id for m in (state.values or {}).get("messages", [])]
            )
        self.seen = set(self.execution.history_ids)

    def check_resume(self, state: Any) -> None:
        # A durable checkpoint cannot tell us whether an interrupted external
        # command already committed its side effect. Resume model work and
        # read-only tool work; fail closed for an uncertain tool step.
        if "tools" in state.next and not state.interrupts:
            messages = (state.values or {}).get("messages", [])
            last_ai = next(
                (m for m in reversed(messages) if isinstance(m, AIMessage)), None
            )
            safe = {"ls", "read_file", "glob", "grep"}
            if last_ai and any(call["name"] not in safe for call in last_ai.tool_calls):
                raise RecoveryRequired(
                    "worker stopped during a tool step; verify its result before retrying"
                )

    def install(
        self, saver: Any, on_checkpoint: Callable[[list[BaseMessage]], Awaitable[None]]
    ) -> None:
        for name in ("aput", "aput_writes"):
            original = getattr(saver, name)

            async def guarded(*args, _original=original, _name=name, **kwargs):
                async with self.execution.guard():
                    result = await _original(*args, **kwargs)
                if _name == "aput":
                    checkpoint = args[1] if len(args) > 1 else kwargs["checkpoint"]
                    await on_checkpoint(
                        checkpoint.get("channel_values", {}).get("messages", [])
                    )
                return result

            setattr(saver, name, guarded)

    async def translate(
        self, message: BaseMessage, translate: Callable[..., Awaitable[None]]
    ) -> None:
        if (
            isinstance(message, (HumanMessage, SystemMessage))
            or message.id in self.seen
        ):
            return
        if not message.id:
            raise RecoveryRequired("checkpoint message has no stable identity")
        index = 0

        async def emit(event_type, payload):
            nonlocal index
            key = f"{message.id}:{index}:{event_type}"
            index += 1
            return await self.execution.emit(key, event_type, payload)

        await translate(message, emit)
        self.seen.add(message.id)

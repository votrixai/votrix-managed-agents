"""Credentialed end-to-end acceptance smoke for the controlled VMA pilot.

The target service must be running with real Postgres, object storage, E2B,
and a model provider.  The smoke provisions one disposable sandbox, exercises
an append and two turns (therefore an E2B pause/reconnect), downloads generated
outputs, and then removes the Session and uploaded Files.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import time
from contextlib import suppress
from typing import Any

from anthropic import AsyncAnthropic


MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
FILES_BETAS = ["files-api-2025-04-14", MANAGED_AGENTS_BETA]
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"


def _stop_reason(session: Any) -> dict[str, Any] | None:
    value = getattr(session, "stop_reason", None)
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value if isinstance(value, dict) else None


async def _wait_for_end_turn(
    client: AsyncAnthropic,
    session_id: str,
    *,
    after_seq: int,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_state: tuple[str, dict[str, Any] | None] | None = None
    while time.monotonic() < deadline:
        session = await client.beta.sessions.retrieve(session_id)
        status = str(getattr(session, "status", ""))
        reason = _stop_reason(session)
        last_state = (status, reason)
        events = [
            event
            async for event in client.beta.sessions.events.list(
                session_id,
                limit=100,
                extra_query={"after_seq": after_seq},
            )
        ]
        for event in events:
            if event.type != "session.status_idle" or event.seq <= after_seq:
                continue
            event_reason = getattr(event, "stop_reason", None)
            if hasattr(event_reason, "model_dump"):
                event_reason = event_reason.model_dump(mode="json")
            if isinstance(event_reason, dict) and event_reason.get("type") == "end_turn":
                return
        if status == "terminated":
            raise RuntimeError(f"Session terminated during smoke: {reason}")
        await asyncio.sleep(1)
    raise TimeoutError(f"Session did not finish within {timeout}s; last state={last_state}")


async def _scoped_files(client: AsyncAnthropic, session_id: str) -> list[Any]:
    return [
        item
        async for item in client.beta.files.list(
            scope_id=session_id,
            limit=100,
            betas=FILES_BETAS,
        )
    ]


async def _assert_output(
    client: AsyncAnthropic,
    session_id: str,
    *,
    filename: str,
    marker: bytes,
    timeout: float = 30,
) -> str:
    deadline = time.monotonic() + timeout
    matches: list[Any] = []
    while time.monotonic() < deadline:
        matches = [
            item
            for item in await _scoped_files(client, session_id)
            if getattr(item, "filename", None) == filename
        ]
        if matches:
            break
        await asyncio.sleep(1)
    if not matches:
        raise RuntimeError(
            f"Expected generated output was not exported within {timeout}s: {filename}"
        )
    output = matches[0]
    response = await client.beta.files.download(output.id, betas=FILES_BETAS)
    content = await response.read()
    if marker not in content:
        raise RuntimeError(f"Generated output {filename} did not contain the smoke marker")
    return output.id


async def _delete_session_with_retry(
    client: AsyncAnthropic,
    session_id: str,
    *,
    timeout: float = 30,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await client.beta.sessions.delete(session_id)
            return
        except Exception as exc:  # Cleanup must also tolerate a finishing turn.
            last_error = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"Failed to delete smoke Session {session_id}") from last_error


async def run(*, base_url: str, api_key: str, model: str, timeout: float) -> None:
    marker = f"vma-pilot-{secrets.token_hex(8)}"
    suffix = marker.rsplit("-", 1)[-1]
    session_id: str | None = None
    agent_id: str | None = None
    environment_id: str | None = None
    skill_id: str | None = None
    uploaded_ids: list[str] = []
    output_ids: list[str] = []

    client = AsyncAnthropic(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        max_retries=0,
        _strict_response_validation=True,
    )
    try:
        skill = await client.beta.skills.create(
            display_title=f"VMA pilot smoke {suffix}",
            files=[
                (
                    "pilot-smoke/SKILL.md",
                    (
                        "---\n"
                        "name: pilot-smoke\n"
                        "description: Create the exact files requested by the pilot smoke.\n"
                        "---\n"
                        "Use filesystem tools when the user asks you to read or write a path.\n"
                    ).encode(),
                    "text/markdown",
                )
            ],
        )
        skill_id = skill.id
        agent = await client.beta.agents.create(
            name=f"VMA pilot smoke {suffix}",
            model=model,
            system=(
                "This is an automated infrastructure acceptance test. Follow filesystem "
                "instructions exactly, create every requested file, and answer briefly."
            ),
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ],
            skills=[{"type": "custom", "skill_id": skill.id, "version": "latest"}],
        )
        agent_id = agent.id
        environment = await client.beta.environments.create(
            name=f"VMA pilot smoke {suffix}",
            config={"type": "cloud"},
        )
        environment_id = environment.id

        initial = await client.beta.files.upload(
            file=("initial.txt", f"initial {marker}\n".encode(), "text/plain"),
            betas=FILES_BETAS,
        )
        uploaded_ids.append(initial.id)
        session = await client.beta.sessions.create(
            agent=agent.id,
            environment_id=environment.id,
            resources=[
                {
                    "type": "file",
                    "file_id": initial.id,
                    "mount_path": "/mnt/session/uploads/initial.txt",
                }
            ],
        )
        session_id = session.id

        appended = await client.beta.files.upload(
            file=("appended.txt", f"appended {marker}\n".encode(), "text/plain"),
            betas=FILES_BETAS,
        )
        uploaded_ids.append(appended.id)
        await client.beta.sessions.resources.add(
            session.id,
            type="file",
            file_id=appended.id,
            mount_path="/mnt/session/uploads/appended.txt",
        )

        first_turn = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Read /mnt/session/uploads/initial.txt and "
                                "/mnt/session/uploads/appended.txt. Write the marker "
                                f"{marker} to /workspace/resume-marker.txt and to the direct "
                                "file /mnt/session/outputs/pilot-turn-one.txt."
                            ),
                        }
                    ],
                }
            ],
            extra_headers={"Idempotency-Key": f"{marker}-turn-1"},
        )
        await _wait_for_end_turn(
            client,
            session.id,
            after_seq=first_turn.data[-1].seq,
            timeout=timeout,
        )
        output_ids.append(
            await _assert_output(
                client,
                session.id,
                filename="pilot-turn-one.txt",
                marker=marker.encode(),
            )
        )

        second_turn = await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "After reconnecting the same sandbox, read "
                                "/workspace/resume-marker.txt and copy its exact marker to the "
                                "direct file /mnt/session/outputs/pilot-turn-two.txt."
                            ),
                        }
                    ],
                }
            ],
            extra_headers={"Idempotency-Key": f"{marker}-turn-2"},
        )
        await _wait_for_end_turn(
            client,
            session.id,
            after_seq=second_turn.data[-1].seq,
            timeout=timeout,
        )
        output_ids.append(
            await _assert_output(
                client,
                session.id,
                filename="pilot-turn-two.txt",
                marker=marker.encode(),
            )
        )
        print("VMA controlled-pilot acceptance passed")
    finally:
        if session_id is not None:
            # Capture every Session-scoped immutable copy/output while the
            # Session can still be listed, then kill the E2B sandbox first.
            with suppress(Exception):
                output_ids.extend(item.id for item in await _scoped_files(client, session_id))
            await _delete_session_with_retry(client, session_id)
        for file_id in dict.fromkeys([*output_ids, *uploaded_ids]):
            with suppress(Exception):
                await client.beta.files.delete(file_id, betas=FILES_BETAS)
        if environment_id is not None:
            with suppress(Exception):
                await client.beta.environments.delete(environment_id)
        if agent_id is not None:
            with suppress(Exception):
                await client.beta.agents.archive(agent_id)
        if skill_id is not None:
            with suppress(Exception):
                await client.beta.skills.delete(skill_id)
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("VMA_SMOKE_BASE_URL", "http://127.0.0.1:8080"),
    )
    parser.add_argument(
        "--api-key",
        default=(
            os.environ.get("VMA_SMOKE_API_KEY")
            or os.environ.get("VOTRIX_API_KEY")
        ),
    )
    parser.add_argument("--model", default=os.environ.get("VMA_SMOKE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if not args.api_key:
        parser.error(
            "--api-key, VMA_SMOKE_API_KEY, or VOTRIX_API_KEY is required"
        )
    asyncio.run(
        run(
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    main()

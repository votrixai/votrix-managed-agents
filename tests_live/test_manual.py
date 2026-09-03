"""Rebecca, from votrix-backend, driven by hand on this server.

Nothing here asserts. It is the Social Media Specialist agent as that service
defines it — its `PROMPT.md`, three of its skills, its image and video tools —
loaded onto this runtime with a person in the loop instead of a script. Which is
the only way to find out whether an agent written for one runtime works on this
one: the failures are things like a skill calling a tool that is not here, or a
tool schema the skill's instructions no longer match, and no assertion would have
predicted either.

Two files are the conversation:

    manual_io/input.md    you write, and saving is what sends it. Emptied once read.
    manual_io/output.md   what came back, appended as it happens.

Run it, then leave the editor open:

    MANUAL=1 uv run pytest tests_live/test_manual.py -s

Write `/quit` to stop, or walk away — it gives up after thirty idle minutes.

Three things about this runtime are not votrix-backend's, and the prompt is told
about all three (see `RUNTIME_NOTE`): there is no memory directory, only three of
the ten skills are installed, and nothing can be scheduled or published. The
model is `deepseek-v4-pro` rather than the `claude-sonnet-4-6` in `config.json`
— a prompt written for one model driven by another is part of what this is for.
DeepSeek is text-only, so the prompt is also told it cannot look at what it
generates; the skills' visual-confirmation steps go to the admin instead.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests_live import manual_tools
from tests_live.helpers import (
    AGENT_CUSTOM_TOOL_USE,
    AGENT_MESSAGE,
    AGENT_TOOL_RESULT,
    AGENT_TOOL_USE,
    SESSION_ERROR,
    allow,
    calls,
    message,
    output_files,
    respond,
    send,
    stop_reason,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

BACKEND = Path(
    os.environ.get(
        "VOTRIX_BACKEND_DIR", Path(__file__).resolve().parents[2] / "votrix-backend"
    )
)
AGENT_DIR = BACKEND / "agents" / "social-media-specialist" / "vma"

# The three the run is about. `planner` decides what to post, `content-creation`
# produces one item of it, `generate-video` is the one that reaches for the tools.
SKILL_NAMES = (
    "social-media-post-planner",
    "social-media-post-content-creation",
    "social-media-post-generate-video",
)

# A directory per run, because two runs sharing one input file is a race with a
# silent loser: the second one to start empties `input.md` on the way up, and a
# message typed into the first is gone before it was ever read. `latest` is a
# symlink to whichever run is newest, so an editor can stay open on one path.
IO_ROOT = Path(__file__).parent / "manual_io"
LATEST = IO_ROOT / "latest"

POLL_SECONDS = 1.0
IDLE_TIMEOUT_SECONDS = 30 * 60
# Tool rounds inside one turn, not messages. A video is one round; a shot list
# with three of them plus first frames is a dozen. Generous, and finite.
MAX_ROUNDS = 40

RUNTIME_NOTE = """

---

## This Runtime

You are running on a test harness rather than the Votrix production backend, and
some of what the skills above assume is not here. What actually holds:

- **No memory.** There is no `mnt/memory/` directory. Skip the session-start
  memory read entirely, and ask the admin for anything you would have recalled.
- **Three skills are installed:** `social-media-post-planner`,
  `social-media-post-content-creation`, `social-media-post-generate-video`.
  Nothing else — no setup, no profile, no publishing, no analytics, no
  engagement, no poster-design. Do the work those three cover and say plainly
  when something falls outside them.
- **Two generation tools:** `image_generate` and `video_generate`. To make a
  first frame, call `image_generate` directly rather than routing through a
  generate-image skill, which is not installed.
- **You cannot inspect images.** Your model is text-only, so ask the admin to
  review generated frames whenever a visual judgment is needed. Do not invent
  details about an image you cannot see.
- **You cannot watch video.** For a generated clip, give the admin the URL.
- **Nothing can be published or scheduled.** There are no `cron_*` tools, no
  `schedule_once`, and no connected social accounts. Stop at the approved draft,
  and tell the admin that is where you stopped.
- **Deliverables go in `/home/user/outputs/`.** Files written there are collected
  when the turn ends; files written anywhere else are not.
- **Show the admin `local_path`, not the URL.** Every generated asset comes back
  with three locations: `path` inside the container (yours), `url` (a signed link
  that stops working in ten minutes, and only ever works copied whole — the part
  after `?` is the signature), and `local_path`, a file on the admin's own
  machine. Give them `local_path`. If you do quote a URL, quote all of it.
"""


# --- what the agent is made of ----------------------------------------------


def _zip_dir(root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(root).as_posix())
    return buffer.getvalue()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def smm_skills(api: httpx.AsyncClient) -> Any:
    """The skill packages, uploaded from votrix-backend's own directory."""
    if not AGENT_DIR.is_dir():
        pytest.skip(f"votrix-backend not found at {BACKEND} — set VOTRIX_BACKEND_DIR")

    ids: list[str] = []
    for name in SKILL_NAMES:
        response = await api.post(
            "/v1/skills",
            files={"file": (f"{name}.zip", _zip_dir(BACKEND / "skills" / name), "application/zip")},
        )
        response.raise_for_status()
        ids.append(response.json()["id"])
    yield ids
    for skill_id in ids:
        await api.delete(f"/v1/skills/{skill_id}")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def smm_environment(api: httpx.AsyncClient) -> Any:
    """ffmpeg, because the video skill's last step is a text layer.

    Declaring a package means an image build, which is minutes — and a fresh
    organization per run means there is no earlier one to reuse. It is the price
    of the skill working end to end rather than stopping one step short.
    """
    response = await api.post(
        "/v1/environments",
        json={
            "name": f"manual-smm-{uuid.uuid4().hex[:8]}",
            "config": {"packages": {"apt": ["ffmpeg"]}, "cpu": 2, "memory_mb": 2048},
        },
    )
    response.raise_for_status()
    environment = response.json()

    deadline = time.monotonic() + 900
    while environment["build_state"] == "building":
        if time.monotonic() > deadline:
            pytest.fail(f"environment {environment['id']} still building after 15 minutes")
        await asyncio.sleep(10.0)
        polled = await api.get(f"/v1/environments/{environment['id']}")
        polled.raise_for_status()
        environment = polled.json()
    assert environment["build_state"] == "ready", environment

    yield environment["id"]
    await api.post(f"/v1/environments/{environment['id']}/archive")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def smm_agent(api: httpx.AsyncClient, smm_skills: list[str]) -> str:
    config = json.loads((AGENT_DIR / "config.json").read_text())
    response = await api.post(
        "/v1/agents",
        json={
            "name": config["name"],
            "model": "deepseek-v4-pro",
            "description": config.get("description"),
            "system": (AGENT_DIR / "PROMPT.md").read_text() + RUNTIME_NOTE,
            "tools": [
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {"permission_policy": {"type": "always_allow"}},
                },
                {"type": "web_toolset_20260401"},
                *manual_tools.DEFINITIONS,
            ],
            "skills": [{"skill_id": skill_id} for skill_id in smm_skills],
        },
    )
    response.raise_for_status()
    return response.json()["id"]


@pytest_asyncio.fixture(loop_scope="session")
async def manual_session(new_session: Any, smm_agent: str, smm_environment: str) -> str:
    """No mounted resources: whatever this session works from, it makes."""
    return await new_session(
        agent_id=smm_agent, environment_id=smm_environment, resources=[]
    )


# --- the two files ----------------------------------------------------------


class Transcript:
    """One run's pair of files, and the `latest` symlink pointing at them."""

    def __init__(self, session_id: str) -> None:
        stamp = datetime.now().strftime("%m%d-%H%M%S")
        self.dir = IO_ROOT / f"{stamp}-{session_id[-6:]}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.input = self.dir / "input.md"
        self.output = self.dir / "output.md"
        self.input.write_text("")
        self.output.write_text("")

        # Relative, so the link keeps working if the checkout moves. Replacing
        # it under an editor that has `latest/input.md` open is the point: the
        # path is resolved again on save, so the next thing typed goes to this
        # run rather than to the one that has finished.
        if LATEST.is_symlink() or LATEST.exists():
            LATEST.unlink()
        LATEST.symlink_to(self.dir.name, target_is_directory=True)

        # Generated assets land here too, so the person reading the transcript
        # can open the image the paragraph above is talking about.
        manual_tools.LOCAL_COPIES = self.dir

    def emit(self, text: str) -> None:
        """Everything the run says goes to both the file and the terminal."""
        with self.output.open("a") as log:
            log.write(text + "\n")
        print(text, flush=True)

    async def next_input(self) -> str | None:
        """Wait for something to be written. `None` means nobody came back."""
        deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            text = self.input.read_text().strip() if self.input.exists() else ""
            if text:
                self.input.write_text("")
                return text
            await asyncio.sleep(POLL_SECONDS)
        return None

    def report(self, events: list[dict[str, Any]], reported: int) -> int:
        """Append whatever is new, and say how far we got."""
        for event in events[reported:]:
            kind = event["type"]
            if kind == AGENT_MESSAGE:
                said = "\n".join(block["text"] for block in event["content"]).strip()
                if said:
                    self.emit(f"\n**Rebecca:** {said}\n")
            elif kind in (AGENT_TOOL_USE, AGENT_CUSTOM_TOOL_USE):
                self.emit(f"- 🔧 `{event['name']}` {_preview(event.get('input') or {})}")
            elif kind == AGENT_TOOL_RESULT:
                body = " ".join(block["text"] for block in event["content"])
                failed = event.get("is_error")
                # A failure gets four times the room. The 240-char preview once
                # cut a provider's error body off after the status code, which
                # cost several retries and a round of guessing at parameters
                # that were never the problem.
                marker = "⚠️" if failed else "↳"
                self.emit(f"  {marker} {_preview(body, 1000 if failed else 240)}")
            elif kind == SESSION_ERROR:
                self.emit(f"\n> ⚠️ session error: {_preview(event)}\n")
        return len(events)


def _preview(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


# --- the loop ---------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MANUAL"),
    reason="hand-driven: run it with MANUAL=1 and -s",
)
async def test_manual(api: httpx.AsyncClient, manual_session: str) -> None:
    transcript = Transcript(manual_session)
    transcript.emit(f"# session {manual_session}\n")
    transcript.emit(f"Write into `{LATEST / 'input.md'}` and save. `/quit` ends it.\n")

    delivered: set[str] = set()
    while True:
        text = await transcript.next_input()
        if text is None:
            transcript.emit("\n> nobody wrote anything for thirty minutes — stopping.\n")
            return
        if text.lower() in ("/quit", "/exit"):
            transcript.emit("\n> `/quit` — stopping.\n")
            return

        transcript.emit(f"\n**You:** {text}\n")
        events = await send(api, manual_session, [message(text)])
        reported = transcript.report(events, 0)

        for _ in range(MAX_ROUNDS):
            reason = stop_reason(events)
            if reason["type"] != "requires_action":
                break
            answers: list[dict[str, Any]] = []
            for tool_use_id in reason["tool_use_ids"]:
                call = next(
                    (c for c in calls(events) if c["tool_use_id"] == tool_use_id), None
                )
                if call is None or call["type"] != AGENT_CUSTOM_TOOL_USE:
                    # A direct tool asking permission. Nobody is here to refuse
                    # one, and the point of the run is what the agent produces.
                    answers.append(allow(tool_use_id))
                    continue
                result, failed = await manual_tools.execute(
                    call["name"],
                    call.get("input") or {},
                    api=api,
                    session_id=manual_session,
                )
                answers.append(respond(tool_use_id, result, is_error=failed))
            events += await send(api, manual_session, answers)
            reported = transcript.report(events, reported)
        else:
            transcript.emit(f"\n> ⚠️ still asking after {MAX_ROUNDS} rounds — moving on.\n")

        produced = await output_files(api, manual_session)
        for name in sorted(set(produced) - delivered):
            transcript.emit(f"- 📄 `{name}` — file {produced[name]}")
        delivered = set(produced)
        transcript.emit(f"\n_turn ended: {stop_reason(events)['type']}_\n")

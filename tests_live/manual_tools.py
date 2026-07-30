"""The tools Rebecca needs, run here rather than by the server.

A `custom` tool has no implementation on this side of the API — the client
answers it — so a manual run of an agent written for votrix-backend has to run
that service's tools itself. `image_generate` and `video_generate` are lifted
from `votrix-backend/app/tools/{image,video}.py` with everything that belongs to
that service stripped out: its R2 bucket, its `generated_media` registry, its
usage metering. What is left is the model call.

Where the bytes land is the one deliberate difference. The backend uploads to R2
and hands the model a permanent public URL; here they go into this account's own
file storage and into the running container, and the model is handed a signed
URL to show the admin.

That URL is for people, not for Ark. Ark runs in cn-beijing and cannot download
from this Cloudflare R2 endpoint — a first frame passed as a URL comes back
`InvalidParameter: resource download failed` every time, while the same URL
fetches fine from here and the same request with the image inlined is accepted.
Whether that is the border or a fetcher that dislikes signed query strings is
not something this side can tell apart — and `_inline` is the right answer to
both, so it sends the bytes rather than an address.

`video_generate` keeps the vocabulary the skill was written against — `image_url`
and `duration_seconds`, from when this tool was Veo-backed — because that is what
the model will send. The Seedance names are accepted too, and the mode is
inferred rather than asked for.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values
from google import genai
from google.genai import types as genai_types

from app.config import get_settings
from app.utils.sandbox import UPLOADS_DIR

# Read, not written down. These were hard-coded while this file was git-ignored;
# it is tracked now, and a key in a tracked file is a key in the history forever.
# Gemini's is this service's own setting; Ark's is votrix-backend's, and lives in
# `.env` rather than in `Settings` because this service has no media tools of its
# own to configure.
# The `.env` read by hand because pydantic-settings parses that file into
# `Settings` without ever putting it in the environment, and `ark_api_key` is
# not one of the fields it parses.
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

GEMINI_API_KEY = get_settings().gemini_api_key
ARK_API_KEY = os.environ.get("ARK_API_KEY") or dotenv_values(_ENV_FILE).get("ARK_API_KEY", "")

GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image-preview"

_RATIO_TO_PIXEL_SIZE = {
    "1:1": "1024x1024",
    "9:16": "1024x1792",
    "16:9": "1792x1024",
    "4:3": "1024x768",
    "3:4": "768x1024",
}

_ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
_ARK_MODEL_BY_TIER = {
    "fast": "doubao-seedance-2-0-fast-260128",
    "standard": "doubao-seedance-2-0-260128",
}
_ARK_TERMINAL = {"succeeded", "failed", "expired", "cancelled"}

# Signed URLs we have issued, so one can be re-signed when it comes back to us
# as an input. Keyed by the URL itself because that is what the model holds.
_ISSUED: dict[str, str] = {}

# Where to drop a copy of everything generated, set by the run that owns the
# transcript. The person driving this is sitting at this machine: a path they
# can open beats a signed URL that expires in ten minutes and stops working the
# moment anything trims its query string.
LOCAL_COPIES: Path | None = None


DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "custom",
        "name": "image_generate",
        "description": (
            "Generate an image from a text prompt with optional reference images using "
            "Gemini 3.1 Flash. Best for photorealistic product photos and high-fidelity "
            "images. Returns a URL to the generated image and its path in the sandbox."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to generate or edit — subject, setting, visual details, style.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "9:16", "16:9", "4:3", "3:4"],
                    "description": "Defaults to 1:1. 9:16 Stories/Reels, 16:9 YouTube/LinkedIn.",
                },
                "reference_image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Image URLs to use as references. Max 14.",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "type": "custom",
        "name": "video_generate",
        "description": (
            "Generate a short video with native audio (dialogue, SFX, ambient). Pass "
            "`image_url` to animate a still image as the video's first frame, which is "
            "the usual way to use this tool. Generation takes 1-5 minutes. Returns a URL "
            "to the MP4 and its path in the sandbox."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed description of the video: scene, action, mood, camera "
                        "movement, dialogue and audio cues."
                    ),
                },
                "image_url": {
                    "type": "string",
                    "description": "Image to animate as the video's first frame.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
                    "description": "Defaults to auto. 9:16 for Reels and Shorts.",
                },
                "duration_seconds": {
                    "type": "integer",
                    "description": "4-15 seconds. Defaults to 5.",
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "720p"],
                    "description": "Defaults to 720p.",
                },
                "generate_audio": {
                    "type": "boolean",
                    "description": "Generate synchronized audio. Defaults to true.",
                },
                "model": {
                    "type": "string",
                    "enum": ["fast", "standard"],
                    "description": "Tier. 'fast' (default) for lower latency, 'standard' for highest quality.",
                },
                "reference_image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Reference images to compose into the scene instead of anchoring a "
                        "first frame (max 9). Cite them in the prompt as @Image1, @Image2."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        # votrix-backend's version hands the file to a channel this harness does
        # not have. This one does the part that matters here: the file leaves the
        # container for storage, and the admin gets a link they can open.
        "type": "custom",
        "name": "present_file_to_user",
        "description": (
            "Deliver a file the session produced to the admin. Pass its path inside "
            "the sandbox, e.g. /home/user/outputs/draft.md. The file is uploaded out "
            "of the container and you get back a link to it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
]

NAMES = tuple(definition["name"] for definition in DEFINITIONS)


async def execute(
    name: str, arguments: dict[str, Any], *, api: httpx.AsyncClient, session_id: str
) -> tuple[str, bool]:
    """Run one call. Returns what the model should see and whether it failed.

    A tool that raises comes back as `is_error`, not as a successful result whose
    text happens to say it went wrong — the model reasons about the second one as
    data and carries on building on top of an image that does not exist.
    """
    try:
        if name == "image_generate":
            return await _image(arguments, api=api, session_id=session_id), False
        if name == "video_generate":
            return await _video(arguments, api=api, session_id=session_id), False
        if name == "present_file_to_user":
            return await _present(arguments, api=api, session_id=session_id), False
    except Exception as exc:
        return json.dumps({"status": False, "message": f"{type(exc).__name__}: {exc}"}), True
    return json.dumps({"status": False, "message": f"No such tool: {name}"}), True


# --- image -------------------------------------------------------------------


async def _image(
    arguments: dict[str, Any], *, api: httpx.AsyncClient, session_id: str
) -> str:
    prompt = str(arguments.get("prompt") or "").strip()
    aspect_ratio = str(arguments.get("aspect_ratio") or "1:1")
    references = [
        await _refresh(api, url) for url in (arguments.get("reference_image_urls") or [])
    ]

    size = _RATIO_TO_PIXEL_SIZE.get(aspect_ratio, "1024x1024")
    contents: list[Any] = []
    for url in references[:14]:
        data, mime = await _fetch(url, "image/jpeg")
        contents.append(genai_types.Part.from_bytes(data=data, mime_type=mime))
    contents.append(
        genai_types.Part.from_text(text=f"{prompt}\n\nImage dimensions: {size}. High quality.")
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = await client.aio.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=contents,
        config=genai_types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            data, mime = part.inline_data.data, part.inline_data.mime_type
            break
    else:
        raise RuntimeError("Gemini returned no image")

    extension = mime.split("/")[-1] or "png"
    filename = f"generated_{uuid.uuid4().hex[:8]}.{extension}"
    url, path, local = await _land(api, session_id, filename, mime, data)
    return json.dumps(
        {
            "status": True,
            "url": url,
            "path": path,
            "local_path": local,
            "aspect_ratio": aspect_ratio,
        }
    )


# --- video -------------------------------------------------------------------


async def _video(
    arguments: dict[str, Any], *, api: httpx.AsyncClient, session_id: str
) -> str:
    prompt = str(arguments.get("prompt") or "")
    aspect_ratio = str(arguments.get("aspect_ratio") or "auto")
    first_frame = arguments.get("image_url") or arguments.get("first_frame_url")
    references = arguments.get("reference_image_urls") or []
    requested = arguments.get("duration_seconds") or arguments.get("duration") or 5
    duration = 5 if str(requested) == "auto" else max(4, min(15, int(requested)))

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if first_frame:
        mode = "image_to_video"
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": await _inline(api, str(first_frame))},
                "role": "first_frame",
            }
        )
    elif references:
        mode = "reference_to_video"
        for url in references[:9]:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": await _inline(api, str(url))},
                    "role": "reference_image",
                }
            )
    else:
        mode = "text_to_video"

    body = {
        "model": _ARK_MODEL_BY_TIER.get(str(arguments.get("model") or "fast"), _ARK_MODEL_BY_TIER["fast"]),
        "content": content,
        "resolution": str(arguments.get("resolution") or "720p"),
        "ratio": "adaptive" if aspect_ratio == "auto" else aspect_ratio,
        "duration": duration,
        "generate_audio": arguments.get("generate_audio", True),
        "watermark": False,
    }
    data = await _seedance(body)

    filename = f"generated_{uuid.uuid4().hex[:8]}.mp4"
    url, path, local = await _land(api, session_id, filename, "video/mp4", data)
    return json.dumps(
        {
            "status": True,
            "url": url,
            "path": path,
            "local_path": local,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "mode": mode,
        }
    )


async def _seedance(body: dict[str, Any], *, timeout: float = 900.0) -> bytes:
    headers = {"Authorization": f"Bearer {ARK_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        submitted = await client.post(_ARK_URL, headers=headers, json=body)
        if submitted.status_code >= 400:
            # `raise_for_status` throws away the body, and the body is the whole
            # message: Ark answers a rejected request with which field it
            # disliked. Without it a 400 is unactionable, to us and to the model.
            raise RuntimeError(
                f"Ark rejected the request ({submitted.status_code}): {submitted.text[:600]}"
            )
        task_id = submitted.json().get("id")
        if not task_id:
            raise RuntimeError(f"No task id in Ark response: {submitted.json()}")

        deadline = time.monotonic() + timeout
        while True:
            polled = await client.get(f"{_ARK_URL}/{task_id}", headers=headers)
            polled.raise_for_status()
            task = polled.json()
            if task.get("status") in _ARK_TERMINAL:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Ark task {task_id} unfinished after {timeout:.0f}s, "
                    f"last status {task.get('status')}"
                )
            await asyncio.sleep(5.0)

    if task.get("status") != "succeeded":
        raise RuntimeError(
            f"Ark task {task_id} {task.get('status')}: {task.get('error') or 'no detail'}"
        )
    data, _ = await _fetch(task["content"]["video_url"], "video/mp4", timeout=180.0)
    return data


# --- delivering what the agent made -----------------------------------------


async def _present(
    arguments: dict[str, Any], *, api: httpx.AsyncClient, session_id: str
) -> str:
    """Take a file out of the container and hand back a link to it.

    The reverse of `_land`, and it goes the other way for a reason: the
    container uploads to storage itself against a presigned PUT, so a 30MB
    video never travels through this process.

    The link is signed and short-lived. This account's bucket is private on
    purpose — there is no permanent public URL to hand out, and inventing one
    would mean opening the bucket.
    """
    from app.db.engine import session_scope
    from app.db.queries import sessions as sessions_q
    from app.utils.sandbox import Sandbox

    path = str(arguments.get("file_path") or "").strip()
    if not path:
        return json.dumps({"status": False, "message": "file_path is required"})

    async with session_scope() as db:
        row = await sessions_q.get_session_by_id(db, session_id=session_id)
        if row is None:
            return json.dumps({"status": False, "message": f"session {session_id} is gone"})
        sandbox = await sessions_q.get_sandbox(
            db, session_id=session_id, organization_id=row.organization_id
        )
        if sandbox is None or not sandbox.external_sandbox_id:
            return json.dumps({"status": False, "message": "this session has no container"})
        container = Sandbox.from_id(
            sandbox.external_sandbox_id, session_id, row.organization_id
        )
        try:
            stored = await container.download_file(db, path, scope_id=session_id)
        except ValueError:
            return json.dumps({"status": False, "message": f"no file at {path}"})
        await db.commit()

    return json.dumps(
        {
            "status": True,
            "url": await _sign(api, stored.id),
            "file_id": stored.id,
            "filename": stored.filename,
            "size_bytes": stored.size_bytes,
            "url_expires_in_seconds": get_settings().transfer_url_ttl_seconds,
        }
    )


# --- where the bytes go ------------------------------------------------------


async def _land(
    api: httpx.AsyncClient, session_id: str, filename: str, mime: str, data: bytes
) -> tuple[str, str | None, str | None]:
    """Store the file, sign it, and put a copy everywhere it is wanted.

    Three destinations, for three readers: the account's storage so there is a
    record, the container so the agent can open it, and the transcript folder so
    the person driving the run can double-click it.
    """
    created = await api.post("/v1/files", files={"file": (filename, data, mime)})
    created.raise_for_status()
    file_id = created.json()["id"]

    local: str | None = None
    if LOCAL_COPIES is not None:
        try:
            beside_the_transcript = LOCAL_COPIES / filename
            beside_the_transcript.write_bytes(data)
            local = str(beside_the_transcript)
        except OSError:
            pass  # A convenience copy is not worth failing a generation over.

    return await _sign(api, file_id), await _into_sandbox(session_id, file_id, filename), local


async def _sign(api: httpx.AsyncClient, file_id: str) -> str:
    redirect = await api.get(f"/v1/files/{file_id}/content")
    assert redirect.status_code == 307, f"{redirect.status_code}: {redirect.text}"
    url = redirect.headers["location"]
    _ISSUED[url] = file_id
    return url


async def _refresh(api: httpx.AsyncClient, url: str) -> str:
    """Re-sign a URL of ours, for a caller that fetches it from this process."""
    file_id = _ISSUED.get(url)
    return await _sign(api, file_id) if file_id else url


async def _inline(api: httpx.AsyncClient, url: str) -> str:
    """Hand Ark the bytes, not somewhere to go and get them.

    Ark runs in cn-beijing and could not download our Cloudflare R2 URLs at all
    — every attempt came back `InvalidParameter: resource download failed`,
    while the same URL fetched fine from here and the same request with the
    image inlined was accepted. A signed URL was never going to work for a
    fetcher on the other side of that border, and a public bucket would only
    move the problem to whether *this* bucket is reachable from *that* network.

    Inlining also removes the expiry race: a frame generated twenty minutes ago
    is still just bytes.
    """
    if not url.startswith("http"):
        return url  # already a data URI, or something we should not touch
    try:
        file_id = _ISSUED.get(url)
        fresh = await _sign(api, file_id) if file_id else url
        data, mime = await _fetch(fresh, "image/jpeg")
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        # Better to let Ark try the URL and say why than to fail here.
        return url


async def _into_sandbox(session_id: str, file_id: str, filename: str) -> str | None:
    """Same trip the session's own resources take, made mid-turn.

    Best effort: an asset the agent cannot open locally is still an asset it has
    a URL for, and failing the tool call over the copy would throw away a video
    that took five minutes to make.
    """
    from app.db.engine import session_scope
    from app.db.queries import sessions as sessions_q
    from app.utils.sandbox import Sandbox

    path = f"{UPLOADS_DIR}/{filename}"
    try:
        async with session_scope() as db:
            row = await sessions_q.get_session_by_id(db, session_id=session_id)
            if row is None:
                return None
            sandbox = await sessions_q.get_sandbox(
                db, session_id=session_id, organization_id=row.organization_id
            )
            if sandbox is None or not sandbox.external_sandbox_id:
                return None
            container = Sandbox.from_id(
                sandbox.external_sandbox_id, session_id, row.organization_id
            )
            await container.upload_file(db, path, file_id)
        return path
    except Exception:
        return None


async def _fetch(url: str, fallback_mime: str, *, timeout: float = 60.0) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        mime = response.headers.get("content-type", fallback_mime).split(";")[0].strip()
        return response.content, mime or fallback_mime

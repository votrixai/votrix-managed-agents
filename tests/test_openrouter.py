"""The gateway receives the multimodal blocks LangChain says it receives."""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openrouter import OpenRouter

from app.utils.openrouter import VMAChatOpenRouter

ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ONE_PIXEL_JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0o"
    "OjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8a"
    "Gi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2P/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAA"
    "AAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAABQb/xAAUEQEAAAAAAAAAAAAA"
    "AAAAAAAA/9oADAMBAAIRAxEAPwCKALXj/9k="
)
ONE_PIXEL_GIF = "R0lGODdhAQABAIEAAP8AAAAAAAAAAAAAACwAAAAAAQABAAAIBAABBAQAOw=="
ONE_PIXEL_WEBP = "UklGRhwAAABXRUJQVlA4TA8AAAAvAAAAAAcQ/Y/+ByKi/wEA"


CASES = [
    (
        {
            "type": "image",
            "base64": ONE_PIXEL_PNG,
            "mime_type": "image/jpeg",
        },
        f"data:image/png;base64,{ONE_PIXEL_PNG}",
    ),
    (
        {
            "type": "image",
            "base64": ONE_PIXEL_JPEG,
            "mime_type": "image/png",
        },
        f"data:image/jpeg;base64,{ONE_PIXEL_JPEG}",
    ),
    (
        {
            "type": "image",
            "base64": ONE_PIXEL_GIF,
            "mime_type": "image/png",
        },
        f"data:image/gif;base64,{ONE_PIXEL_GIF}",
    ),
    (
        {
            "type": "image",
            "base64": ONE_PIXEL_WEBP,
            "mime_type": "image/png",
        },
        f"data:image/webp;base64,{ONE_PIXEL_WEBP}",
    ),
    (
        {"type": "image", "url": "https://images.example.test/chart.png"},
        "https://images.example.test/chart.png",
    ),
]


@pytest.mark.contract
@pytest.mark.parametrize(
    ("image_block", "expected_url"),
    CASES,
    ids=["png-magic", "jpeg-magic", "gif-magic", "webp-magic", "url"],
)
def test_read_file_image_reaches_the_openrouter_wire_as_an_image_url(
    image_block, expected_url
):
    """Exercise LangChain and the official SDK, stopping only at HTTP."""

    requests: list[dict] = []
    request_bodies: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        request_bodies.append(body)
        requests.append(json.loads(body))
        return httpx.Response(
            200,
            json={
                "id": "gen_image_test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "I can see it."},
                        "finish_reason": "stop",
                    }
                ],
                "created": 1,
                "model": "openai/gpt-4o-mini",
                "object": "chat.completion",
                "system_fingerprint": None,
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(respond))
    sdk = OpenRouter(
        api_key="sk-or-v1-test",
        server_url="https://openrouter.test",
        client=http_client,
    )
    model = VMAChatOpenRouter(
        model="openai/gpt-4o-mini",
        api_key="sk-or-v1-test",
        client=sdk,
        max_retries=0,
    )
    call = {
        "id": "call_image",
        "name": "read_file",
        "args": {"file_path": "/image.png"},
    }
    original_block = dict(image_block)
    tool_message = ToolMessage(
        content_blocks=[image_block],
        name="read_file",
        tool_call_id=call["id"],
        id="tool_message_image",
        artifact={"path": "/image.png"},
        additional_kwargs={"read_file_path": "/image.png"},
        status="success",
    )

    result = model.invoke([AIMessage(content="", tool_calls=[call]), tool_message])

    assert result.content == "I can see it."
    assert len(requests) == 1
    wire_message = requests[0]["messages"][-1]
    assert wire_message == {
        "role": "tool",
        "tool_call_id": "call_image",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": expected_url},
            }
        ],
    }
    assert '"type":"image"' not in request_bodies[0]
    assert '"type":"UNKNOWN"' not in request_bodies[0]

    # Formatting the request must not replace or mutate the LangChain message.
    assert tool_message.name == "read_file"
    assert tool_message.tool_call_id == "call_image"
    assert tool_message.id == "tool_message_image"
    assert tool_message.artifact == {"path": "/image.png"}
    assert tool_message.additional_kwargs == {"read_file_path": "/image.png"}
    assert tool_message.content == [original_block]

    http_client.close()


def test_text_only_model_omits_checkpointed_read_file_image():
    model = VMAChatOpenRouter(
        model="deepseek/deepseek-v4-pro",
        api_key="sk-or-v1-test",
    )
    image = {
        "type": "image",
        "base64": ONE_PIXEL_PNG,
        "mime_type": "image/png",
    }
    tool_message = ToolMessage(
        content_blocks=[image],
        name="read_file",
        tool_call_id="call_image",
    )

    messages, _ = model._create_message_dicts(
        [tool_message, HumanMessage("Continue.")], stop=None
    )

    assert model.profile and model.profile["image_inputs"] is False
    assert messages[0] == {
        "role": "tool",
        "tool_call_id": "call_image",
        "content": [
            {"type": "text", "text": "[Image omitted: text-only model.]"}
        ],
    }
    assert messages[1] == {"role": "user", "content": "Continue."}
    assert tool_message.content == [image]

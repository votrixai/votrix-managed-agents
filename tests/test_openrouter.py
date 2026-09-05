"""The gateway receives the multimodal blocks LangChain says it receives."""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from openrouter import OpenRouter

from app.runtime.openrouter import VMAChatOpenRouter

ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.mark.contract
def test_read_file_image_reaches_the_openrouter_wire_as_an_image_url():
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
    tool_message = ToolMessage(
        content_blocks=[
            {
                "type": "image",
                "base64": ONE_PIXEL_PNG,
                "mime_type": "image/png",
            }
        ],
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
                "image_url": {"url": f"data:image/png;base64,{ONE_PIXEL_PNG}"},
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

    http_client.close()

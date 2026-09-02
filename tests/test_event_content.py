"""User-message content blocks accepted at the Session API boundary."""

import pytest
from pydantic import ValidationError

from app.models.events import SendEventsRequest
from app.runtime.engine import _build_fresh_input


IMAGE_MESSAGE = {
    "type": "user.message",
    "content": [
        {"type": "text", "text": "What changed in this screenshot?"},
        {
            "type": "image",
            "source": {"type": "file", "file_id": "file_image"},
        },
    ],
}


def test_a_user_message_accepts_a_durable_image_reference():
    request = SendEventsRequest.model_validate({"events": [IMAGE_MESSAGE]})

    assert request.model_dump() == {"events": [IMAGE_MESSAGE]}


@pytest.mark.parametrize(
    "source",
    [
        {"type": "base64", "data": "aGVsbG8=", "media_type": "image/png"},
        {"type": "url", "url": "https://example.com/image.png"},
    ],
)
def test_image_bytes_and_external_urls_do_not_enter_the_event_log(source):
    with pytest.raises(ValidationError):
        SendEventsRequest.model_validate(
            {
                "events": [
                    {
                        "type": "user.message",
                        "content": [{"type": "image", "source": source}],
                    }
                ]
            }
        )


def test_graph_state_keeps_the_file_id_instead_of_image_bytes():
    graph_input = _build_fresh_input([IMAGE_MESSAGE])

    assert graph_input["messages"][0].content == [
        {"type": "text", "text": "What changed in this screenshot?"},
        {"type": "image", "file_id": "file_image"},
    ]


def test_text_only_graph_input_remains_a_string():
    graph_input = _build_fresh_input(
        [
            {
                "type": "user.message",
                "content": [{"type": "text", "text": "hello"}],
            }
        ]
    )

    assert graph_input["messages"][0].content == "hello"

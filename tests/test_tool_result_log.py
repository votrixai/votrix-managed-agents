"""Tool results stay useful without turning the event log into blob storage."""

from app.runtime.engine import _readable_tool_result


def test_image_tool_result_is_summarized_without_base64():
    encoded = "YWI="

    result = _readable_tool_result(
        [{"type": "image", "base64": encoded, "mime_type": "image/jpeg"}]
    )

    assert result == "[image/jpeg, 2 bytes]"
    assert encoded not in result

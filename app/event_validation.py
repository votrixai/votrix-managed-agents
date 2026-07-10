from fastapi import HTTPException

SYSTEM_MESSAGE_PREDECESSORS = {"user.custom_tool_result", "user.message", "user.tool_result"}
MAX_OUTCOME_RUBRIC_TEXT_CHARS = 262_144
MAX_OUTCOME_ITERATIONS = 20


def validate_system_message_batch(event_types: list[str]) -> None:
    system_indexes = [index for index, event_type in enumerate(event_types) if event_type == "system.message"]
    if not system_indexes:
        return
    if len(system_indexes) > 1:
        raise HTTPException(status_code=422, detail="At most one system.message event is allowed per request")
    system_index = system_indexes[0]
    if system_index != len(event_types) - 1:
        raise HTTPException(status_code=422, detail="system.message must be the final event in the request")
    if system_index == 0 or event_types[system_index - 1] not in SYSTEM_MESSAGE_PREDECESSORS:
        raise HTTPException(
            status_code=422,
            detail="system.message must immediately follow user.message, user.tool_result, or user.custom_tool_result",
        )


def validate_user_define_outcome_event(payload: dict) -> None:
    description = payload.get("description") or payload.get("objective")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=422, detail="user.define_outcome requires a non-empty description")

    rubric = payload.get("rubric")
    if not isinstance(rubric, dict):
        raise HTTPException(status_code=422, detail="user.define_outcome requires a rubric object")
    rubric_type = rubric.get("type")
    if rubric_type == "text":
        content = rubric.get("content")
        if not isinstance(content, str) or not content:
            raise HTTPException(status_code=422, detail="user.define_outcome rubric.content must be a non-empty string")
        if len(content) > MAX_OUTCOME_RUBRIC_TEXT_CHARS:
            raise HTTPException(status_code=422, detail="user.define_outcome rubric.content is too long")
    elif rubric_type == "file":
        file_id = rubric.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise HTTPException(status_code=422, detail="user.define_outcome rubric.file_id must be a non-empty string")
    else:
        raise HTTPException(status_code=422, detail='user.define_outcome rubric.type must be "text" or "file"')

    if payload.get("max_iterations") is not None:
        try:
            max_iterations = int(payload["max_iterations"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="user.define_outcome max_iterations must be an integer") from exc
        if max_iterations < 1 or max_iterations > MAX_OUTCOME_ITERATIONS:
            raise HTTPException(status_code=422, detail="user.define_outcome max_iterations must be between 1 and 20")

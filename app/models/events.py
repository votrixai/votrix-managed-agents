import json
from datetime import datetime
from typing import Any

from pydantic import Field

from app.db.models import SessionEvent
from app.models.common import ApiModel, FlexibleApiModel


class SessionEventInput(FlexibleApiModel):
    type: str


class SendEventsRequest(ApiModel):
    events: list[SessionEventInput] = Field(min_length=1)


class SessionEventResponse(FlexibleApiModel):
    id: str
    type: str
    session_id: str
    seq: int
    created_at: datetime
    processed_at: datetime | None = None


class SendEventsResponse(ApiModel):
    data: list[SessionEventResponse]


def event_to_response(event: SessionEvent) -> SessionEventResponse:
    data = dict(event.payload)
    data.update(
        {
            "id": event.id,
            "type": event.type,
            "session_id": event.session_id,
            "seq": event.seq,
            "created_at": event.created_at,
        }
    )
    return SessionEventResponse.model_validate(data)


def event_to_sse(event: SessionEvent) -> str:
    public = event_to_response(event).model_dump(mode="json")
    return (
        f"id: {event.seq}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(public, separators=(',', ':'))}\n\n"
    )

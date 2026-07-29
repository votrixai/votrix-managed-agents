import pytest
from pydantic import ValidationError

from app.models.sessions import SessionCreateRequest


def test_session_create_funding_is_optional_for_compatibility() -> None:
    request = SessionCreateRequest(
        agent="agent_compatible",
        environment_id="env_compatible",
    )

    assert request.funding is None
    assert "funding" not in request.model_dump(exclude_none=True)


@pytest.mark.parametrize(
    "funding_type",
    ["organization_default", "byok", "platform_credits"],
)
def test_session_create_accepts_native_funding_types(funding_type: str) -> None:
    request = SessionCreateRequest.model_validate(
        {
            "agent": "agent_native",
            "environment_id": "env_native",
            "funding": {"type": funding_type},
        }
    )

    assert request.funding is not None
    assert request.funding.type == funding_type
    assert request.model_dump(mode="json")["funding"] == {"type": funding_type}


def test_session_create_rejects_unknown_funding_type() -> None:
    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate(
            {
                "agent": "agent_native",
                "environment_id": "env_native",
                "funding": {"type": "unknown"},
            }
        )

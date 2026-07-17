from app.session_errors import normalize_session_error_payload, session_error_payload


def test_runtime_session_error_is_sdk_shaped_and_preserves_legacy_metadata():
    payload = session_error_payload(
        "provider unavailable",
        error_type="ProviderRuntimeError",
        retry_status="retrying",
        transient=True,
        attempt=2,
    )

    assert payload == {
        "type": "session.error",
        "message": "provider unavailable",
        "error_type": "ProviderRuntimeError",
        "transient": True,
        "attempt": 2,
        "error": {
            "type": "unknown_error",
            "message": "provider unavailable",
            "retry_status": {"type": "retrying"},
        },
    }


def test_mcp_session_errors_map_to_sdk_variants():
    auth_error = session_error_payload(
        "credential missing",
        error_type="mcp_auth_missing",
        retry_status="exhausted",
        mcp_server_name="github",
    )
    connection_error = session_error_payload(
        "connection failed",
        error_type="mcp_connection_error",
        retry_status="exhausted",
        mcp_server_name="notion",
    )

    assert auth_error["error"] == {
        "type": "mcp_authentication_failed_error",
        "message": "credential missing",
        "retry_status": {"type": "exhausted"},
        "mcp_server_name": "github",
    }
    assert connection_error["error"] == {
        "type": "mcp_connection_failed_error",
        "message": "connection failed",
        "retry_status": {"type": "exhausted"},
        "mcp_server_name": "notion",
    }


def test_legacy_session_error_normalization_is_idempotent():
    legacy = {
        "type": "session.error",
        "message": "rate limited",
        "error_type": "TransientRuntimeError",
        "transient": True,
        "attempt": 1,
    }

    normalized = normalize_session_error_payload(legacy)

    assert normalized["error"] == {
        "type": "unknown_error",
        "message": "rate limited",
        "retry_status": {"type": "retrying"},
    }
    assert normalize_session_error_payload(normalized) == normalized


def test_incomplete_structured_error_falls_back_to_unknown_sdk_variant():
    normalized = normalize_session_error_payload(
        {
            "type": "session.error",
            "error": {
                "type": "mcp_connection_failed_error",
                "message": "missing server name",
                "retry_status": {"type": "terminal"},
            },
        }
    )

    assert normalized["error"]["type"] == "unknown_error"
    assert normalized["error"]["retry_status"] == {"type": "terminal"}

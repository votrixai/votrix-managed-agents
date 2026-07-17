import sys

import structlog

from app.logging import add_cloud_logging_severity, redact_secrets


def test_logging_redacts_secret_fields_recursively():
    event = {
        "api_key": "sk-test",
        "nested": {
            "access_token": "secret-token",
            "safe": "value",
        },
        "items": [
            {"client_secret": "client-secret"},
            {"public": "visible"},
        ],
    }

    redacted = redact_secrets(None, "info", event)

    assert redacted["api_key"] == "redacted"
    assert redacted["nested"]["access_token"] == "redacted"
    assert redacted["nested"]["safe"] == "value"
    assert redacted["items"][0]["client_secret"] == "redacted"
    assert redacted["items"][1]["public"] == "visible"


def test_exception_processor_renders_traceback_instead_of_boolean_flag():
    try:
        raise RuntimeError("checkpoint import failed")
    except RuntimeError:
        event = {"event": "session_run_failed", "exc_info": sys.exc_info()}

    rendered = structlog.processors.format_exc_info(None, "exception", event)

    assert "exc_info" not in rendered
    assert "RuntimeError: checkpoint import failed" in rendered["exception"]


def test_cloud_logging_severity_uses_structlog_level():
    event = add_cloud_logging_severity(None, "error", {"level": "error"})

    assert event["severity"] == "ERROR"

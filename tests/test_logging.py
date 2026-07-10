from app.logging import redact_secrets


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

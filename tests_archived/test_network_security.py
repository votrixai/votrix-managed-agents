from __future__ import annotations

import socket

import pytest
from fastapi import HTTPException

from app.agent_contract import validate_mcp_bindings
from app.network_security import (
    _PinnedPublicNetworkBackend,
    create_restricted_http_client,
    validate_public_https_url,
    validate_public_https_url_syntax,
)


@pytest.mark.parametrize(
    "url",
    (
        "http://mcp.example.com/sse",
        "https://user:secret@mcp.example.com/sse",
        "https://mcp.example.com:8443/sse",
        "https://localhost/sse",
        "https://metadata.google.internal/computeMetadata/v1",
        "https://127.0.0.1/sse",
        "https://10.0.0.1/sse",
        "https://169.254.169.254/computeMetadata/v1",
        "https://[::1]/sse",
    ),
)
def test_public_mcp_url_syntax_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_https_url_syntax(url)


def test_agent_mcp_contract_rejects_unsafe_url_before_persistence() -> None:
    with pytest.raises(HTTPException) as exc_info:
        validate_mcp_bindings(
            [
                {
                    "type": "url",
                    "name": "private",
                    "url": "https://127.0.0.1/sse",
                }
            ],
            [{"type": "mcp_toolset", "mcp_server_name": "private"}],
        )

    assert exc_info.value.status_code == 422
    assert "Invalid MCP server URL" in str(exc_info.value.detail)


async def test_public_mcp_url_rejects_private_dns_answers(monkeypatch) -> None:
    def private_answer(*_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.20.30.40", 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", private_answer)

    with pytest.raises(ValueError, match="private or non-global"):
        await validate_public_https_url("https://mcp.example.com/sse")


async def test_public_mcp_url_requires_every_dns_answer_to_be_global(monkeypatch) -> None:
    def mixed_answer(*_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 443),
            ),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_answer)

    with pytest.raises(ValueError, match="private or non-global"):
        await validate_public_https_url("https://mcp.example.com/sse")


async def test_public_mcp_url_accepts_a_global_dns_answer(monkeypatch) -> None:
    def public_answer(*_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", public_answer)

    await validate_public_https_url("https://mcp.example.com/sse")


async def test_restricted_mcp_client_disables_redirects_and_proxy_environment() -> None:
    client = create_restricted_http_client()
    try:
        assert client.follow_redirects is False
        assert client._trust_env is False
    finally:
        await client.aclose()


async def test_connection_backend_pins_the_validated_ip(monkeypatch) -> None:
    def public_answer(*_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

    class RecordingBackend:
        def __init__(self) -> None:
            self.hosts: list[str] = []

        async def connect_tcp(self, host, port, **_kwargs):
            self.hosts.append(host)
            return (host, port)

    monkeypatch.setattr(socket, "getaddrinfo", public_answer)
    delegate = RecordingBackend()
    backend = _PinnedPublicNetworkBackend(delegate)

    stream = await backend.connect_tcp("mcp.example.com", 443)

    assert stream == ("93.184.216.34", 443)
    assert delegate.hosts == ["93.184.216.34"]

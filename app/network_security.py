from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx


_BLOCKED_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.google.internal.",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


def validate_public_https_url_syntax(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("Invalid HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Only absolute HTTPS URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain embedded credentials")
    if port != 443:
        raise ValueError("Public MCP URLs must use HTTPS port 443")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _BLOCKED_HOSTS or hostname.endswith(_BLOCKED_SUFFIXES):
        raise ValueError("URL hostname is not publicly routable")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("URL hostname is invalid") from exc
    else:
        if not address.is_global:
            raise ValueError("URL resolves to a private or non-global address")
    return hostname, port


async def validate_public_https_url(value: str) -> None:
    hostname, port = validate_public_https_url_syntax(value)
    await resolve_public_addresses(hostname, port)


async def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve once, reject mixed answers, and return addresses safe to pin.

    Checking every answer matters: accepting one public answer while silently
    retaining a private answer leaves clients vulnerable to resolver ordering
    changes and DNS rebinding.
    """

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            0,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("URL hostname could not be resolved") from exc
    if not infos:
        raise ValueError("URL hostname could not be resolved")
    addresses: list[str] = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("URL resolves to a private or non-global address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    return tuple(addresses)


class _PinnedPublicNetworkBackend:
    """httpcore network backend that connects to the address it validated.

    httpcore still supplies the original hostname when it wraps the resulting
    stream in TLS, so certificate validation and SNI remain correct while the
    TCP connection cannot perform a second, attacker-controlled DNS lookup.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def connect_tcp(
        self,
        host: str | bytes,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ):
        hostname = host.decode("ascii") if isinstance(host, bytes) else host
        if port != 443:
            raise ValueError("Outbound public connections must use HTTPS port 443")
        addresses = await resolve_public_addresses(hostname, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ValueError("URL hostname could not be resolved")

    async def connect_unix_socket(self, *_args, **_kwargs):
        raise ValueError("Unix sockets are not permitted for public HTTP connections")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedPublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        super().__init__(trust_env=False, retries=0)
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise RuntimeError("Installed httpx/httpcore does not support the pinned public transport")
        pool._network_backend = _PinnedPublicNetworkBackend(backend)


def create_restricted_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        transport=_PinnedPublicHTTPTransport(),
        follow_redirects=False,
        trust_env=False,
    )

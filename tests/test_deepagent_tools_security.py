import httpx
import pytest

from app.config import get_settings
from app.runtime import deepagent_tools


async def test_bounded_response_reader_stops_after_limit() -> None:
    response = httpx.Response(200, content=b"0123456789")

    body, truncated = await deepagent_tools._read_bounded_body(response, max_bytes=4)

    assert body == b"0123"
    assert truncated is True


async def test_web_fetch_streams_and_truncates_response(monkeypatch) -> None:
    monkeypatch.setenv("VMA_WEB_FETCH_MAX_BYTES", "4")
    get_settings.cache_clear()

    async def allow_test_url(_value: str, *, allow_private: bool) -> None:
        assert allow_private is False

    def client_factory(*, allow_private: bool) -> httpx.AsyncClient:
        assert allow_private is False
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=b"0123456789",
                    headers={"content-type": "text/plain; charset=utf-8"},
                    request=request,
                )
            )
        )

    monkeypatch.setattr(deepagent_tools, "_validate_public_url", allow_test_url)
    monkeypatch.setattr(deepagent_tools, "_outbound_http_client", client_factory)

    result = await deepagent_tools.web_fetch_tool().ainvoke(
        {"url": "https://public.example/data"}
    )

    assert result.startswith("0123")
    assert "Response truncated" in result


@pytest.mark.parametrize("allow_private", [False, True])
async def test_web_url_validation_rejects_embedded_credentials(allow_private: bool) -> None:
    with pytest.raises(ValueError, match="embedded credentials"):
        await deepagent_tools._validate_public_url(
            "https://user:password@example.com/data",
            allow_private=allow_private,
        )

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from curl_cffi import CurlError, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from test_api import page, profile

from thm_bot.api import BASE, THMClient
from thm_bot.models import SchemaError, THMError
from thm_bot.transport import MAX_API_BYTES, BrowserAPITransport


class CurlSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.close = AsyncMock()

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        body, status, headers = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        for offset in range(0, len(body), 65536):
            if kwargs["content_callback"](body[offset : offset + 65536]) == CURL_WRITEFUNC_ERROR:
                raise CurlError("aborted", 23)
        return SimpleNamespace(
            status_code=status,
            headers=headers,
            request=SimpleNamespace(headers={"User-Agent": "test-browser"}),
        )


def response(data, status=200):
    return data, status, {"Content-Type": "application/json"}


async def test_real_adapter_routes_all_pages_through_browser_session(config):
    curl = CurlSession(
        response({"data": profile(3)}),
        response({"data": page(["z", "a"], 3, 2, True)}),
        response({"data": page(["b"], 3, 2, False)}),
        response({"data": profile(3)}),
    )
    images = SimpleNamespace(get=AsyncMock())
    async with BrowserAPITransport(config, session=curl) as transport:
        snapshot = await THMClient(images, config, api_session=transport).snapshot("ysami5061")
        assert len(snapshot.rooms) == 3
        assert curl.calls[2][1]["params"]["page"] == 2
        assert all(call[1]["discard_cookies"] for call in curl.calls)
        assert all(call[1]["allow_redirects"] is False for call in curl.calls)
        assert transport.last_user_agent == "test-browser"
    curl.close.assert_awaited_once()
    images.get.assert_not_called()


async def test_transport_429_is_not_hidden_or_retried_by_another_transport(config):
    curl = CurlSession(response({"error": {"code": "challenge"}}, 429))
    images = SimpleNamespace(get=AsyncMock())
    async with BrowserAPITransport(config, session=curl) as transport:
        client = THMClient(images, config, api_session=transport)
        for _ in range(2):
            with pytest.raises(THMError) as error:
                await client.profile("ysami5061")
            assert error.value.kind == "access_challenge"
        assert len(curl.calls) == 1
    images.get.assert_not_called()


async def test_transport_body_callback_enforces_decompressed_byte_limit(config):
    curl = CurlSession(response(b"x" * (MAX_API_BYTES + 1)))
    async with BrowserAPITransport(config, session=curl) as transport:
        with pytest.raises(SchemaError):
            async with transport.get(BASE):
                pytest.fail("Oversized body accepted")


async def test_transport_network_errors_are_safe_and_compatible_with_existing_backoff(config):
    curl = CurlSession(response(CurlError("private upstream detail", 7)))
    async with BrowserAPITransport(config, session=curl) as transport:
        with pytest.raises(aiohttp.ClientError) as error:
            async with transport.get(BASE):
                pytest.fail("Network failure accepted")
        assert "private upstream detail" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://tryhackme.com/api/v2/public-profile",
        "https://127.0.0.1/",
        "https://tryhackme.com@evil.example/api/v2/public-profile",
        "https://tryhackme.com/api/v2/users/self",
        BASE + "?username=someone",
        BASE + "#fragment",
    ],
)
async def test_transport_only_allows_the_two_fixed_public_routes(config, url):
    curl = CurlSession()
    async with BrowserAPITransport(config, session=curl) as transport:
        with pytest.raises(THMError):
            async with transport.get(url):
                pytest.fail("Unexpected destination accepted")
    assert not curl.calls


async def test_browser_session_uses_matching_automatic_ua_and_discards_cookies(config, monkeypatch):
    from thm_bot import transport as module

    captured = {}
    fake = CurlSession()

    def factory(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(module, "AsyncSession", factory)
    async with BrowserAPITransport(config):
        pass
    assert captured["impersonate"] == "chrome"
    assert captured["discard_cookies"] is True
    assert captured["trust_env"] is False
    assert captured["timeout"] == config.timeout
    assert captured["curl_options"][CurlOpt.MAXFILESIZE_LARGE] == MAX_API_BYTES
    assert captured["curl_options"][CurlOpt.CONNECTTIMEOUT_MS] <= 10000
    assert "Cookie" not in captured["headers"]
    assert "Authorization" not in captured["headers"]
    assert "User-Agent" not in captured["headers"]

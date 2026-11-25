import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock

import aiohttp
import pytest

from thm_bot.api import THMClient, image_url_allowed, retry_seconds
from thm_bot.models import SchemaError, THMError


def profile(count):
    return {
        "username": "ysami5061",
        "avatar": None,
        "about": "",
        "isPrivateProfile": False,
        "completedRoomsNumber": count,
    }


def page(codes, total, pages, more):
    return {
        "docs": [{"code": c, "title": c, "imageURL": "", "difficulty": "easy"} for c in codes],
        "totalDocs": total,
        "totalPages": pages,
        "hasNextPage": more,
    }


async def test_reads_every_page_without_order_assumptions(config):
    api = THMClient(None, config)
    api._json = AsyncMock(
        side_effect=[profile(3), page(["z", "a"], 3, 2, True), page(["middle"], 3, 2, False), profile(3)]
    )
    result = await api.snapshot("ysami5061")
    assert {r.code for r in result.rooms} == {"z", "a", "middle"}
    assert api._json.call_args_list[2].kwargs["page"] == 2


async def test_public_stats_and_explicit_os_tags_are_preserved(config):
    stats = {**profile(1), "rank": 120, "totalPoints": 1500, "streak": 3, "level": 10, "badgesNumber": 8}
    rooms = page(["internal"], 1, 1, False)
    rooms["docs"][0].update(difficulty="hard", technologyTags=["Linux", "Web"], type="challenge")
    api = THMClient(None, config)
    api._json = AsyncMock(side_effect=[stats, rooms, stats])
    result = await api.snapshot("ysami5061")
    assert result.profile.total_points == 1500 and result.profile.badges_count == 8
    assert "about" not in result.profile.public_data()
    assert result.rooms[0].operating_system == "Linux"
    assert result.rooms[0].difficulty == "hard"


@pytest.mark.parametrize(
    "bad_page",
    [
        page(["a"], 2, 1, False),  # claimed total exceeds received docs
        page(["a"], 1, 2, False),  # missing next page
        {"docs": []},
        {**page(["a"], 1, 1, False), "totalDocs": True},
        {**page(["a"], 1, 1, False), "hasNextPage": "false"},
        page([], 1, 1, False),
        {**page(["a"], 1, 1, False), "docs": [{"code": "../bad", "title": "bad", "imageURL": ""}]},
    ],
)
async def test_rejects_partial_or_changed_schema(config, bad_page):
    api = THMClient(None, config)
    api._json = AsyncMock(side_effect=[profile(1), bad_page, profile(1)])
    with pytest.raises(SchemaError):
        await api.snapshot("ysami5061")


async def test_duplicate_page_and_metadata_changes_rejected(config):
    for last in [page(["a"], 2, 2, False), page(["b"], 3, 2, False)]:
        api = THMClient(None, config)
        api._json = AsyncMock(side_effect=[profile(2), page(["a"], 2, 2, True), last])
        with pytest.raises(SchemaError):
            await api.snapshot("ysami5061")


async def test_count_changes_mid_snapshot_rejected(config):
    api = THMClient(None, config)
    api._json = AsyncMock(side_effect=[profile(1), page(["a"], 1, 1, False), profile(2)])
    with pytest.raises(SchemaError):
        await api.snapshot("ysami5061")


@pytest.mark.parametrize("pages", [0, 1])
async def test_genuinely_empty_public_account(config, pages):
    api = THMClient(None, config)
    api._json = AsyncMock(side_effect=[profile(0), page([], 0, pages, False), profile(0)])
    assert not (await api.snapshot("ysami5061")).rooms


@pytest.mark.parametrize(
    "payload",
    [
        {**profile(0), "isPrivateProfile": True},
        {**profile(0), "isPrivateProfile": None},
        {**profile(0), "username": "someone_else"},
        {"username": "ysami5061"},
    ],
)
async def test_private_unavailable_and_wrong_account_fail_closed(config, payload):
    api = THMClient(None, config)
    api._json = AsyncMock(return_value=payload)
    with pytest.raises(THMError):
        await api.snapshot("ysami5061")


class Response:
    def __init__(self, payload=None, status=200, headers=None, raw=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.raw = raw if raw is not None else json.dumps(payload).encode()
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.raw), size):
            yield self.raw[offset : offset + size]


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


async def test_429_respects_header_and_globally_stops_further_requests(config):
    session = Session(Response(status=429, headers={"Retry-After": "180"}))
    api = THMClient(session, config)
    with pytest.raises(THMError) as first:
        await api.profile("ysami5061")
    assert first.value.retry_after >= 180
    with pytest.raises(THMError):
        await api.profile("another")
    assert len(session.calls) == 1


async def test_http_429_without_header_increases_backoff(config):
    api = THMClient(Session(Response(status=429), Response(status=429)), config)
    for delay in (60, 120):
        api.blocked_until = 0
        with pytest.raises(THMError) as error:
            await api.profile("ysami5061")
        assert error.value.retry_after == delay


async def test_challenge_is_distinguished_from_rate_limit_and_cached(config):
    session = Session(
        Response(
            {"error": {"code": "challenge", "message": "private upstream detail"}},
            status=429,
            headers={"Retry-After": "180"},
        )
    )
    api = THMClient(session, config)
    for _ in range(2):
        with pytest.raises(THMError) as error:
            await api.profile("ysami5061")
        assert error.value.kind == "access_challenge"
        assert "challenge" in str(error.value)
        assert "private upstream detail" not in str(error.value)
        assert error.value.retry_after > 170
    assert len(session.calls) == 1


async def test_large_429_body_is_bounded_and_still_respects_cooldown(config):
    api = THMClient(Session(Response(raw=b"x" * 20000, status=429)), config)
    with pytest.raises(THMError) as error:
        await api.profile("ysami5061")
    assert error.value.kind == "rate_limited"
    assert error.value.retry_after == 60
    with pytest.raises(THMError):
        await api.profile("ysami5061")
    assert len(api.session.calls) == 1


async def test_429_body_timeout_does_not_retry_or_lose_retry_after(config):
    class BodyTimeout(Response):
        async def iter_chunked(self, size):
            raise TimeoutError()
            yield b""  # Make this an async iterator without yielding data.

    api = THMClient(Session(BodyTimeout(status=429, headers={"Retry-After": "300"})), config)
    with pytest.raises(THMError) as error:
        await api.profile("ysami5061")
    assert error.value.retry_after == 300
    assert len(api.session.calls) == 1


async def test_network_failures_retry_bounded_and_recover(config, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    api = THMClient(
        Session(aiohttp.ClientError(), Response(status=503), Response({"data": profile(0)})), config
    )
    assert (await api.profile("ysami5061")).completed_count == 0
    assert len(api.session.calls) == 3
    assert sleep.await_count >= 2


async def test_network_retries_exhausted(config, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    api = THMClient(Session(*[TimeoutError() for _ in range(3)]), config)
    with pytest.raises(THMError, match="three"):
        await api.profile("ysami5061")
    assert len(api.session.calls) == 3


@pytest.mark.parametrize(
    "response",
    [
        Response(raw=b"<html>challenge</html>"),
        Response({"data": []}),
        Response(raw=b"x" * (2097152 + 1)),
        Response(status=403),
        Response(status=302),
    ],
)
async def test_malformed_large_nonjson_and_non200_rejected(config, response):
    with pytest.raises(THMError):
        await THMClient(Session(response), config).profile("ysami5061")


def test_retry_after_http_date_and_not_truncating_large_delay():
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 118 <= retry_seconds(date) <= 121
    assert retry_seconds("7200") == 7200
    assert retry_seconds("bad") == 60


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/a.png",
        "https://127.0.0.1/a",
        "https://cdn.discordapp.com.evil.example/a",
        "https://cdn.discordapp.com@evil.example/a",
        "https://cdn.discordapp.com:8443/a",
        "file:///etc/passwd",
        "https://evil.example/a",
    ],
)
def test_disallowed_image_sources(config, url):
    assert not image_url_allowed(url, config.image_hosts)


async def test_image_bounds_and_redirects(config):
    for response in [
        Response(status=302),
        Response(headers={"Content-Type": "image/png", "Content-Length": "9000000"}),
        Response(headers={"Content-Type": "image/png"}, raw=b"x" * (config.max_image_bytes + 1)),
        Response(headers={"Content-Type": "text/html"}, raw=b"invalid"),
    ]:
        session = Session(response)
        assert await THMClient(session, config).image("https://cdn.discordapp.com/a.png") is None
        assert session.calls[0][1]["allow_redirects"] is False


async def test_image_rate_limit_is_cached(config):
    api = THMClient(Session(Response(status=429, headers={"Retry-After": "200"})), config)
    for _ in range(2):
        assert await api.image("https://cdn.discordapp.com/a.png") is None
    assert len(api.session.calls) == 1

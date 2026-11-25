import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from filelock import FileLock, Timeout
from test_api import Response, Session

from thm_bot import config as config_module
from thm_bot.api import PublicResolver, THMClient, make_session
from thm_bot.config import LEGACY_DEFAULT_HOSTS, Config
from thm_bot.models import THMError


def test_config_paths_defaults_and_token_never_in_validation_message(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "ROOT", tmp_path)
    # Isolate environment so no preexisting token can be used by any test.
    for key in ("DISCORD_TOKEN", "DATABASE_PATH", "DISCORD_DEV_GUILD_ID", "POLL_INTERVAL_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "POLL_INTERVAL_SECONDS=700\nDATABASE_PATH=data/test.sqlite3\n", encoding="utf-8"
    )
    config = Config.load()
    assert config.poll_interval == 700
    assert config.database == tmp_path / "data/test.sqlite3"
    with pytest.raises(ValueError):
        Config.load(require_token=True)
    secret = "private secret with spaces"
    monkeypatch.setenv("DISCORD_TOKEN", secret)
    with pytest.raises(ValueError) as error:
        Config.load(require_token=True)
    assert secret not in str(error.value)


def test_single_process_lock(tmp_path):
    path = tmp_path / "bot.lock"
    with FileLock(path, timeout=0):
        with pytest.raises(Timeout), FileLock(path, timeout=0):
            pass


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["10.0.0.1"], ["::1"], ["169.254.169.254"], ["1.1.1.1", "192.168.0.1"]]
)
async def test_dns_private_and_mixed_answers_blocked(addresses):
    resolver = PublicResolver()
    original = resolver.inner
    resolver.inner = SimpleNamespace(
        resolve=AsyncMock(return_value=[{"host": address} for address in addresses])
    )
    try:
        with pytest.raises(OSError):
            await resolver.resolve("cdn.discordapp.com", 443)
    finally:
        await original.close()


async def test_public_dns_answer_accepted():
    resolver = PublicResolver()
    original = resolver.inner
    resolver.inner = SimpleNamespace(resolve=AsyncMock(return_value=[{"host": "1.1.1.1"}]))
    try:
        assert await resolver.resolve("cdn.discordapp.com", 443) == [{"host": "1.1.1.1"}]
    finally:
        await original.close()


async def test_session_has_timeout_no_cookies_no_environment_proxy(config):
    async with make_session(config) as session:
        assert session.timeout.total == config.timeout
        assert session.timeout.connect <= 10
        assert not session.trust_env
        assert not list(session.cookie_jar)
        assert session.headers["User-Agent"] == (config.user_agent or "THMRoomAchievementsBot/1.2")
        assert session.headers["Accept-Language"] == "en-US,en;q=0.9"
        assert session.headers["Accept"] == "application/json, text/plain, */*"


@pytest.mark.parametrize(
    "value", ["\t", "Test\r\nCookie: secret", "x" * 513, "\u062a\u062c\u0631\u0628\u0629"]
)
def test_custom_headers_reject_controls_oversized_and_non_ascii(monkeypatch, tmp_path, value):
    monkeypatch.setattr(config_module, "ROOT", tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("THM_USER_AGENT", value)
    with pytest.raises(ValueError, match="THM_USER_AGENT"):
        Config.load()


async def test_valid_custom_request_headers_are_used(config):
    config = replace(config, user_agent="ExampleClient/2.0", accept_language="ar,en;q=0.8")
    async with make_session(config) as session:
        assert session.headers["User-Agent"] == "ExampleClient/2.0"
        assert session.headers["Accept-Language"] == "ar,en;q=0.8"
        assert "Cookie" not in session.headers
        assert "Authorization" not in session.headers


async def test_same_origin_images_and_api_share_cooldown(config):
    session = Session(Response(status=429, headers={"Retry-After": "120"}))
    api = THMClient(session, config)
    assert await api.image("https://tryhackme.com/image.png") is None
    with pytest.raises(THMError):
        await api.profile("ysami5061")
    assert len(session.calls) == 1


async def test_api_calls_are_paced(config, monkeypatch):
    sleeps = []

    async def record(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record)
    config = replace(config, request_interval=2)
    api = THMClient(Session(Response({"data": {}}), Response({"data": {}})), config)
    await api._json()
    await api._json()
    assert sleeps[-1] > 1.5


def test_legacy_default_allowlist_automatically_allows_verified_room_cdn(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "ROOT", tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("IMAGE_ALLOWED_HOSTS", ",".join(sorted(LEGACY_DEFAULT_HOSTS)))
    assert "cdn-images.tryhackme.com" in Config.load().image_hosts


def test_custom_restricted_allowlist_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "ROOT", tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("IMAGE_ALLOWED_HOSTS", "cdn.discordapp.com")
    assert Config.load().image_hosts == frozenset({"cdn.discordapp.com"})

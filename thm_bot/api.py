"""Strict, bounded access to unofficial public endpoints. Fail closed on drift."""

import asyncio
import ipaddress
import json
import math
import random
import re
import socket
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .models import Profile, Room, SchemaError, Snapshot, THMError

USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
ROOM_CODE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
BASE = "https://tryhackme.com/api/v2/public-profile"


def username(value: str) -> str:
    value = value.strip()
    if not USERNAME.fullmatch(value):
        raise THMError("Invalid TryHackMe username. Enter the username only, without a URL.")
    return value


def retry_seconds(value: str | None, default: float = 60) -> float:
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = default
    # Never shorten a valid server-provided delay.
    return max(1, delay) if delay == delay and delay != float("inf") else default


class PublicResolver(AbstractResolver):
    """Block DNS answers pointing to private/reserved networks (also for images)."""

    def __init__(self):
        self.inner = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        answers = await self.inner.resolve(host, port, family)
        if not answers or any(not ipaddress.ip_address(a["host"]).is_global for a in answers):
            raise OSError("Non-public destination blocked")
        return answers

    async def close(self):
        await self.inner.close()


def image_url_allowed(url: str, hosts: frozenset[str]) -> bool:
    try:
        parsed = urlsplit(url)
        try:
            ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            pass
        else:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname in hosts
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and len(url) <= 4096
            and not re.search(r"[\x00-\x20\x7f]", url)
        )
    except ValueError:
        return False


class THMClient:
    def __init__(self, session, config, *, api_session=None):
        self.session, self.config = session, config
        self.api_session = api_session if api_session is not None else session
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.rate_failures = 0
        self.block_kind = "rate_limited"
        self.image_lock = asyncio.Lock()
        self.image_next = 0.0
        self.image_blocked = {}

    async def _challenge_response(self, response):
        if response.headers.get("X-Vercel-Mitigated", "").casefold() == "challenge":
            return True
        # Read a bounded error envelope only. Never log arbitrary upstream text.
        raw = bytearray()
        try:
            async for chunk in response.content.iter_chunked(2048):
                if len(raw) + len(chunk) > 16384:
                    return False
                raw.extend(chunk)
            payload = json.loads(raw)
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("error"), dict)
            and payload["error"].get("code") == "challenge"
        )

    def _blocked_error(self, delay):
        wait = math.ceil(delay)
        if self.block_kind == "access_challenge":
            message = (
                "TryHackMe rejected automated access (HTTP 429 / challenge). "
                f"Wait at least {wait} seconds. If this continues, access must be allowed by TryHackMe. "
                "Changing the Biography code will not fix this. Completion history was not updated."
            )
        else:
            message = (
                f"TryHackMe returned HTTP 429. Wait at least {wait} seconds before trying again. "
                "Completion history was not updated."
            )
        return THMError(message, delay, kind=self.block_kind)

    async def _json(self, path="", **params):
        for attempt in range(3):
            async with self.lock:
                now = time.monotonic()
                if self.blocked_until > now:
                    raise self._blocked_error(self.blocked_until - now)
                await asyncio.sleep(max(0, self.next_request - now))
                self.next_request = time.monotonic() + self.config.request_interval
                try:
                    async with self.api_session.get(
                        BASE + path, params=params, allow_redirects=False
                    ) as response:
                        if response.status == 429:
                            self.rate_failures += 1
                            delay = retry_seconds(
                                response.headers.get("Retry-After"),
                                min(3600, 60 * 2 ** min(self.rate_failures - 1, 6)),
                            )
                            self.blocked_until = time.monotonic() + delay
                            self.block_kind = (
                                "access_challenge"
                                if await self._challenge_response(response)
                                else "rate_limited"
                            )
                            raise self._blocked_error(delay)
                        if response.status >= 500:
                            raise aiohttp.ClientError("Upstream server failure")
                        if response.status != 200:
                            raise THMError(
                                f"Could not access the TryHackMe account (HTTP {response.status}).", 600
                            )
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 2 * 1024 * 1024:
                                raise SchemaError(
                                    "The TryHackMe response exceeds the allowed size limit.", 600
                                )
                        try:
                            payload = json.loads(raw)
                        except (ValueError, UnicodeError):
                            raise SchemaError(
                                "TryHackMe did not return valid JSON. The API may have changed.", 600
                            ) from None
                        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                            raise SchemaError(
                                "The TryHackMe response has a missing or incompatible data object.", 600
                            )
                        self.rate_failures = 0
                        return payload["data"]
                except (TimeoutError, aiohttp.ClientError, OSError):
                    if attempt == 2:
                        raise THMError(
                            "Could not connect to TryHackMe after three bounded attempts.", 120
                        ) from None
            await asyncio.sleep(2**attempt + random.uniform(0, 1))

    async def profile(self, name: str) -> Profile:
        name = username(name)
        data = await self._json(username=name)
        required = ("username", "avatar", "about", "isPrivateProfile", "completedRoomsNumber")
        if any(k not in data for k in required):
            raise SchemaError("TryHackMe profile fields are missing or have changed.", 600)
        if type(data["isPrivateProfile"]) is not bool:
            raise SchemaError("The account privacy setting has an unexpected value.", 600)
        if data["isPrivateProfile"]:
            raise THMError("This TryHackMe profile is private. Make it public to resume tracking.", 600)
        if (
            not isinstance(data["username"], str)
            or data["username"].casefold() != name.casefold()
            or not isinstance(data["about"], (str, type(None)))
            or not isinstance(data["avatar"], (str, type(None)))
            or type(data["completedRoomsNumber"]) is not int
            or not 0 <= data["completedRoomsNumber"] <= 100000
        ):
            raise SchemaError("The profile values do not match the expected API schema.", 600)

        def optional_stat(key):
            value = data.get(key)
            return value if type(value) is int and 0 <= value <= 10**15 else None

        return Profile(
            data["username"],
            data["avatar"] or "",
            data["about"] or "",
            data["completedRoomsNumber"],
            rank=optional_stat("rank"),
            total_points=optional_stat("totalPoints"),
            streak=optional_stat("streak"),
            level=optional_stat("level"),
            badges_count=optional_stat("badgesNumber"),
        )

    async def snapshot(self, name: str, *, page_size=100) -> Snapshot:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise THMError("Room page size must be between 1 and 100.")
        before = await self.profile(name)
        rooms = {}
        total = pages = None
        page = 1
        while True:
            data = await self._json("/completed-rooms", username=before.username, limit=page_size, page=page)
            if (
                not isinstance(data.get("docs"), list)
                or type(data.get("totalDocs")) is not int
                or type(data.get("totalPages")) is not int
                or type(data.get("hasNextPage")) is not bool
            ):
                raise SchemaError("The completed-room page has missing or incompatible fields.", 600)
            if page == 1:
                total, pages = data["totalDocs"], data["totalPages"]
                if not 0 <= total <= 100000 or not 0 <= pages <= 1000 or (total and not pages):
                    raise SchemaError("The room page count is invalid or exceeds the safety limit.", 600)
            if (
                total != data["totalDocs"]
                or pages != data["totalPages"]
                or data["hasNextPage"] != (page < pages)
                or (not data["docs"] and total > 0)
            ):
                raise SchemaError(
                    "Room pages are incomplete or changed during the request. They will be checked again later.",
                    120,
                )
            for item in data["docs"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("code"), str)
                    or not ROOM_CODE.fullmatch(item["code"])
                    or not isinstance(item.get("title"), str)
                    or not item["title"].strip()
                    or len(item["title"]) > 2048
                    or not isinstance(item.get("imageURL"), (str, type(None)))
                    or "imageURL" not in item
                    or not isinstance(item.get("difficulty", ""), (str, type(None)))
                ):
                    raise SchemaError(
                        "A room contains fields that do not match the expected API schema.", 600
                    )
                if item["code"] in rooms:
                    raise SchemaError(
                        "A room was repeated across pages. This snapshot cannot be trusted.", 120
                    )
                tags = item.get("technologyTags", [])
                tags = (
                    tuple(t[:128] for t in tags[:100] if isinstance(t, str)) if isinstance(tags, list) else ()
                )
                rooms[item["code"]] = Room(
                    item["code"], item["title"], item["imageURL"] or "", item.get("difficulty") or "", tags
                )
            if not data["hasNextPage"]:
                break
            page += 1
        after = await self.profile(before.username)
        if len(rooms) != total or before.completed_count != total or after.completed_count != total:
            raise SchemaError(
                "The room count does not match the complete snapshot. Tracking state was not updated.", 120
            )
        return Snapshot(after, tuple(rooms.values()))

    async def image(self, url: str) -> bytes | None:
        if not url or not image_url_allowed(url, self.config.image_hosts):
            return None
        host = urlsplit(url).hostname
        async with self.image_lock:
            now = time.monotonic()
            if self.image_blocked.get(host, 0) > now:
                return None
            if host == "tryhackme.com":
                # Image requests to the API origin share its pacing and cooldown.
                async with self.lock:
                    if self.blocked_until > time.monotonic():
                        return None
                    await asyncio.sleep(
                        max(0, self.next_request - time.monotonic(), self.image_next - time.monotonic())
                    )
                    self.next_request = self.image_next = time.monotonic() + self.config.request_interval
                    return await self._download_image(url, host)
            await asyncio.sleep(max(0, self.image_next - now))
            self.image_next = time.monotonic() + self.config.request_interval
            return await self._download_image(url, host)

    async def _download_image(self, url, host):
        try:
            # Redirects are forbidden so an allowed CDN cannot redirect to another host.
            async with self.session.get(
                url, allow_redirects=False, headers={"Accept": "image/*"}
            ) as response:
                if response.status == 429:
                    self.image_blocked[host] = time.monotonic() + retry_seconds(
                        response.headers.get("Retry-After")
                    )
                    if host == "tryhackme.com":
                        self.blocked_until = max(self.blocked_until, self.image_blocked[host])
                    return None
                if response.status != 200 or not response.headers.get("Content-Type", "").startswith(
                    "image/"
                ):
                    return None
                size = response.headers.get("Content-Length")
                if size and int(size) > self.config.max_image_bytes:
                    return None
                result = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    result.extend(chunk)
                    if len(result) > self.config.max_image_bytes:
                        return None
                return bytes(result)
        except (TimeoutError, aiohttp.ClientError, ValueError, OSError):
            return None


def make_session(config):
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.timeout, connect=min(config.timeout, 10)),
        connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=8),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        headers={
            "User-Agent": config.user_agent or "THMRoomAchievementsBot/1.2",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": config.accept_language,
        },
    )

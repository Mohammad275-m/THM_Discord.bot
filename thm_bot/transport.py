"""Bounded, asynchronous browser-compatible HTTP for the two public API routes."""

from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp
from curl_cffi import AsyncSession, CurlECode, CurlError, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from .models import SchemaError, THMError

MAX_API_BYTES = 2 * 1024 * 1024
API_PATHS = {"/api/v2/public-profile", "/api/v2/public-profile/completed-rooms"}


@dataclass
class BufferedResponse:
    status: int
    headers: object
    body: bytes

    @property
    def content(self):
        return self

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class BrowserAPITransport:
    def __init__(self, config, *, session=None):
        self.config = config
        self.last_user_agent = None
        self.request_count = 0
        self.status_counts = Counter()
        headers = {"Accept": "application/json, text/plain, */*", "Accept-Language": config.accept_language}
        if config.user_agent:
            headers["User-Agent"] = config.user_agent
        self.session = (
            session
            if session is not None
            else AsyncSession(
                impersonate="chrome",
                max_clients=1,
                trust_env=False,
                discard_cookies=True,
                verify=True,
                timeout=config.timeout,
                headers=headers,
                curl_options={
                    CurlOpt.CONNECTTIMEOUT_MS: int(min(config.timeout, 10) * 1000),
                    CurlOpt.MAXFILESIZE_LARGE: MAX_API_BYTES,
                },
            )
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self.session.close()

    @asynccontextmanager
    async def get(self, url, *, params=None, allow_redirects=False):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "tryhackme.com"
            or parsed.path not in API_PATHS
            or parsed.query
            or parsed.fragment
            or allow_redirects
        ):
            raise THMError("This API destination or redirect is not allowed.")
        body = bytearray()
        overflow = False

        def collect(chunk):
            nonlocal overflow
            if len(body) + len(chunk) > MAX_API_BYTES:
                overflow = True
                return CURL_WRITEFUNC_ERROR
            body.extend(chunk)
            return len(chunk)

        try:
            self.request_count += 1
            response = await self.session.get(
                url,
                params=params,
                allow_redirects=False,
                discard_cookies=True,
                content_callback=collect,
            )
        except CurlError as error:
            if overflow or error.code == CurlECode.FILESIZE_EXCEEDED:
                raise SchemaError("The TryHackMe response exceeds the allowed size limit.", 600) from None
            raise aiohttp.ClientError("Public API transport failed") from None
        if overflow:
            raise SchemaError("The TryHackMe response exceeds the allowed size limit.", 600)
        self.status_counts[response.status_code] += 1
        request = getattr(response, "request", None)
        if request is not None:
            self.last_user_agent = request.headers.get("User-Agent")
        yield BufferedResponse(response.status_code, response.headers, bytes(body))

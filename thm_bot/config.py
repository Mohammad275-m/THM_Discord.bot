import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USER_AGENT = ""  # Automatic: match the HTTP/TLS browser profile exactly.
DEFAULT_HOSTS = (
    "tryhackme.com,assets.tryhackme.com,cdn.tryhackme.com,cdn-images.tryhackme.com,"
    "tryhackme-images.s3.amazonaws.com,tryhackme-images.s3.eu-west-1.amazonaws.com,"
    "cdn.discordapp.com,media.discordapp.net"
)
LEGACY_DEFAULT_HOSTS = frozenset(DEFAULT_HOSTS.split(",")) - {"cdn-images.tryhackme.com"}


def image_hosts(value):
    hosts = frozenset(host.strip().lower() for host in value.split(",") if host.strip())
    # Upgrade the exact allowlist shipped before TryHackMe's current image CDN was verified.
    # Custom restricted allowlists keep their original meaning.
    if hosts == LEGACY_DEFAULT_HOSTS:
        hosts |= {"cdn-images.tryhackme.com"}
    return hosts


@dataclass(frozen=True)
class Config:
    token: str = ""
    database: Path = ROOT / "data/bot.sqlite3"
    poll_interval: float = 600
    request_interval: float = 2
    timeout: float = 20
    poll_timeout: float = 120
    max_image_bytes: int = 4 * 1024 * 1024
    verify_ttl: int = 900
    font_path: str = ""
    image_hosts: frozenset[str] = frozenset(DEFAULT_HOSTS.split(","))
    dev_guild_id: int | None = None
    user_agent: str = DEFAULT_USER_AGENT
    accept_language: str = "en-US,en;q=0.9"

    @classmethod
    def load(cls, require_token: bool = False):
        load_dotenv(ROOT / ".env", override=False)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_token and not token:
            raise ValueError("Set DISCORD_TOKEN in your local .env file before starting the bot.")
        if token and not re.fullmatch(r"[A-Za-z0-9._-]{20,512}", token):
            raise ValueError("DISCORD_TOKEN has an invalid format. Check it locally without sharing it.")

        def number(key, default, minimum, maximum):
            value = float(os.getenv(key, str(default)))
            if not minimum <= value <= maximum:
                raise ValueError(f"Setting {key} must be between {minimum} and {maximum}.")
            return value

        def header(key, default, *, allow_empty=False):
            value = os.getenv(key, default)
            if allow_empty and value == "":
                return value
            if not re.fullmatch(r"[\x20-\x7e]{1,512}", value) or not value.strip():
                raise ValueError(
                    f"Setting {key} must be a single line of ASCII text without control characters."
                )
            return value

        path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
        return cls(
            token=token,
            database=path if path.is_absolute() else ROOT / path,
            poll_interval=number("POLL_INTERVAL_SECONDS", 600, 60, 86400),
            request_interval=number("REQUEST_INTERVAL_SECONDS", 2, 1, 60),
            timeout=number("HTTP_TIMEOUT_SECONDS", 20, 5, 120),
            poll_timeout=number("POLL_TIMEOUT_SECONDS", 120, 30, 3600),
            max_image_bytes=int(number("MAX_IMAGE_BYTES", 4194304, 1024, 16777216)),
            verify_ttl=int(number("VERIFY_TTL_SECONDS", 900, 60, 3600)),
            font_path=os.getenv("FONT_PATH", ""),
            image_hosts=image_hosts(os.getenv("IMAGE_ALLOWED_HOSTS", DEFAULT_HOSTS)),
            dev_guild_id=int(os.environ["DISCORD_DEV_GUILD_ID"])
            if os.getenv("DISCORD_DEV_GUILD_ID")
            else None,
            user_agent=header("THM_USER_AGENT", DEFAULT_USER_AGENT, allow_empty=True),
            accept_language=header("THM_ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
        )

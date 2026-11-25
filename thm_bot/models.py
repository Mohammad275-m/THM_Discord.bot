from dataclasses import dataclass
from urllib.parse import quote


class THMError(Exception):
    """Safe user-facing error; never includes response bodies or credentials."""

    def __init__(self, message: str, retry_after: float = 60, *, kind: str = "api_error"):
        super().__init__(message)
        self.retry_after = retry_after
        self.kind = kind


class SchemaError(THMError):
    pass


@dataclass(frozen=True)
class Profile:
    username: str
    avatar: str
    about: str
    completed_count: int
    rank: int | None = None
    total_points: int | None = None
    streak: int | None = None
    level: int | None = None
    badges_count: int | None = None

    def public_data(self):
        # Biography can contain ownership codes and must never enter the profile cache.
        return {
            "username": self.username,
            "avatar": self.avatar,
            "completed_count": self.completed_count,
            "rank": self.rank,
            "total_points": self.total_points,
            "streak": self.streak,
            "level": self.level,
            "badges_count": self.badges_count,
        }


@dataclass(frozen=True)
class Room:
    code: str
    title: str
    image_url: str = ""
    difficulty: str = ""
    technology_tags: tuple[str, ...] = ()

    @property
    def operating_system(self):
        names = {
            "linux": "Linux",
            "windows": "Windows",
            "macos": "macOS",
            "mac os": "macOS",
            "android": "Android",
            "ios": "iOS",
        }
        detected = sorted(
            {
                names[t.strip().casefold()]
                for t in self.technology_tags
                if isinstance(t, str) and t.strip().casefold() in names
            }
        )
        return " / ".join(detected) if detected else "Not provided"

    @property
    def url(self):
        return f"https://tryhackme.com/room/{quote(self.code, safe='')}"


@dataclass(frozen=True)
class Snapshot:
    profile: Profile
    rooms: tuple[Room, ...]

"""Live diagnostics. No Discord token required and no Discord messages sent."""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from thm_bot.api import THMClient, make_session, username
from thm_bot.config import Config
from thm_bot.models import THMError
from thm_bot.transport import BrowserAPITransport


async def probe(name, endpoint="snapshot", page_size=100):
    name = username(name)
    config = Config.load()
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "username": name,
        "live_request": True,
        "endpoint": endpoint,
        "discord_messages_sent": 0,
        "transport": "curl_cffi / chrome",
        "page_size": page_size,
        "request_headers": {
            "User-Agent override": config.user_agent or None,
            "Accept-Language": config.accept_language,
        },
    }
    async with make_session(config) as session, BrowserAPITransport(config) as api_session:
        api = THMClient(session, config, api_session=api_session)
        try:
            if endpoint == "completed-rooms":
                data = await api._json("/completed-rooms", username=name, limit=16, page=1)
                result.update(ok=True, data=data)
                return result
            if endpoint == "profile":
                profile = await api.profile(name)
                result.update(
                    ok=True,
                    profile={"username": profile.username, "completed_count": profile.completed_count},
                )
                return result
            snapshot = await api.snapshot(name, page_size=page_size)
            result.update(
                ok=True,
                profile={
                    "username": snapshot.profile.username,
                    "completed_count": snapshot.profile.completed_count,
                },
                rooms=[asdict(r) for r in snapshot.rooms],
            )
        except THMError as error:
            result.update(
                ok=False,
                error=str(error),
                error_kind=error.kind,
                retry_after_seconds=round(error.retry_after),
            )
        finally:
            result["actual_user_agent"] = api_session.last_user_agent
            result["http_request_count"] = api_session.request_count
            result["http_status_counts"] = dict(api_session.status_counts)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True, help="Public TryHackMe username to inspect")
    parser.add_argument("--endpoint", choices=["snapshot", "profile", "completed-rooms"], default="snapshot")
    parser.add_argument("--page-size", type=int, choices=range(1, 101), metavar="1..100", default=100)
    parser.add_argument("--output", default="examples/live-probe.json")
    args = parser.parse_args()
    result = asyncio.run(probe(args.username, args.endpoint, args.page_size))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in ("rooms", "data")}
    summary["report_file"] = str(target.resolve())
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

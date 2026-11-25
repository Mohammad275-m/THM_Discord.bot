"""Optional one-request HTTP transport diagnostic; never uses login cookies."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def probe(name):
    # Optional diagnostic dependency, separate from the running bot.
    from curl_cffi import CurlOpt, requests

    from thm_bot.api import username

    name = username(name)
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "username": name,
        "transport": "curl_cffi",
        "browser_compatibility": "chrome",
        "live_request": True,
        "discord_messages_sent": 0,
        "uses_user_cookies": False,
        "ok": False,
    }
    try:
        with requests.Session(
            impersonate="chrome",
            trust_env=False,
            discard_cookies=True,
            timeout=20,
            curl_options={CurlOpt.MAXFILESIZE_LARGE: 2097152},
        ) as session:
            response = session.get(
                "https://tryhackme.com/api/v2/public-profile",
                params={"username": name},
                headers={"Accept": "application/json, text/plain, */*"},
                allow_redirects=False,
            )
            result.update(
                http_status=response.status_code,
                mitigation=response.headers.get("X-Vercel-Mitigated"),
                retry_after=response.headers.get("Retry-After"),
                content_type=response.headers.get("Content-Type"),
            )
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                result.update(
                    ok=isinstance(data, dict) and data.get("username", "").casefold() == name.casefold(),
                    # Do not save About, avatar, verification tokens or raw responses.
                    observed_fields=sorted(data) if isinstance(data, dict) else [],
                    completed_count=data.get("completedRoomsNumber") if isinstance(data, dict) else None,
                )
    except Exception as error:
        result["error_type"] = type(error).__name__
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True, help="Public TryHackMe username to inspect")
    parser.add_argument("--output", default="examples/live-transport-probe.json")
    args = parser.parse_args()
    result = probe(args.username)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

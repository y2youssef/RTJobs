"""Verify every job-family channel's identity, title and bot permissions.

Read-only Telegram API calls: never sends messages or consumes login updates.
Run .venv/bin/python scripts/check_channels.py --output /tmp/channels.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from config import MARKUP_DIR, TELEGRAM_TOKEN, TELEGRAM_API_BASE_URL
from core.classify import load_channels


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).casefold()
    text = re.sub(r"^\s*rtjobs\b", "", text).replace("🇪🇬", "")
    return re.sub(r"[^a-z0-9]+", "", text)


def audit_channels(channels: dict, families: dict, call) -> dict:
    """Compare current Telegram metadata to canonical family labels."""
    identity = call("getMe", {})
    if not identity.get("ok"):
        raise RuntimeError("Cannot identify bot: " + identity.get("description", "unknown error"))
    bot = identity["result"]
    duplicates = {chat: [key for key, value in channels.items() if value == chat]
                  for chat in channels.values() if list(channels.values()).count(chat) > 1}
    missing = sorted(set(families) - set(channels))

    def check(pair):
        family, chat_id = pair
        row = {"job_family": family, "expected_title": families[family], "expected_id": chat_id, "ok": False}
        response = call("getChat", {"chat_id": chat_id})
        if not response.get("ok"):
            return {**row, "error": response.get("description", "getChat failed")}
        chat = response["result"]
        response = call("getChatMember", {"chat_id": chat_id, "user_id": bot["id"]})
        member = response.get("result") or {}
        title = chat.get("title", "")
        row.update(actual_id=str(chat["id"]), actual_title=title, chat_type=chat.get("type"),
                   id_matches=str(chat["id"]) == chat_id,
                   title_matches=normalize_title(title) == normalize_title(families[family]),
                   branding_matches=bool(re.match(r"^RTJobs\b", title, re.I)) and title.rstrip().endswith("🇪🇬"),
                   can_post=bool(member.get("status") == "creator" or member.get("status") == "administrator"
                                 and member.get("can_post_messages")), membership_status=member.get("status"))
        row["ok"] = all(row[key] for key in ("id_matches", "title_matches", "branding_matches", "can_post")) and chat.get("type") == "channel"
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(check, channels.items()))
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "bot_username": bot.get("username"),
            "expected_categories": len(families), "mapped_channels": len(channels),
            "passed": sum(row["ok"] for row in rows), "missing_families": missing,
            "duplicate_ids": duplicates, "channels": rows,
            "ready": not missing and not duplicates and all(row["ok"] for row in rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    base = TELEGRAM_API_BASE_URL + "/bot" + TELEGRAM_TOKEN

    def call(method, payload):
        for attempt in range(3):
            try:
                response = requests.post(base + "/" + method, json=payload, timeout=15).json()
                if response.get("ok") or response.get("error_code") not in (429, 500, 502, 503):
                    return response
                wait = max(1, int(response.get("parameters", {}).get("retry_after", 2)))
                if wait > 15:
                    return response
            except (requests.RequestException, ValueError, TypeError) as exc:
                # Exception messages can include the token in the request URL.
                response = {"ok": False, "description": type(exc).__name__}
                wait = attempt + 1
            if attempt < 2:
                time.sleep(wait)
        return response

    try:
        families = json.loads((Path(MARKUP_DIR) / "enrichment/taxonomy.json").read_text())["job_families"]
        report = audit_channels(load_channels(), families, call)
    except (ValueError, RuntimeError) as exc:
        print(str(exc))
        return 1
    print("Configured bot: @" + (report["bot_username"] or "unnamed"))
    for row in report["channels"]:
        print("OK" if row["ok"] else "FAIL", row["job_family"], row["expected_id"], "—", row.get("actual_title", row.get("error")))
    print(f"Verified: {report['passed']}/{report['mapped_channels']} mapped channels; expected {report['expected_categories']} categories.")
    if report["missing_families"]:
        print("MISSING:", ", ".join(report["missing_families"]))
    if report["duplicate_ids"]:
        print("DUPLICATE IDS:", json.dumps(report["duplicate_ids"]))
    print("No messages sent.")
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return int(not report["ready"])


if __name__ == "__main__":
    raise SystemExit(main())

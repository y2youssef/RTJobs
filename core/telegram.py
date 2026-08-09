"""Telegram messaging: job notifications (jobs channel) + failure alerts
(dedicated failure channel)."""

import json
import re
import time

import requests

from config import TELEGRAM_CHAT_ID, TELEGRAM_FAILURE_CHAT_ID, TELEGRAM_TOKEN
from core import db

_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text or "")


def _send(
    chat_id: str,
    text: str,
    parse_mode: str = "MarkdownV2",
    max_attempts: int = 3,
) -> bool:
    for attempt in range(max_attempts):
        try:
            resp = requests.post(
                f"{_API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
        except requests.RequestException as e:
            print(f"[telegram] Request error (attempt {attempt + 1}): {e}")
            time.sleep(2)
            continue

        if resp.ok:
            return True

        if resp.status_code == 429:
            wait = resp.json().get("parameters", {}).get("retry_after", 30)
            print(f"[telegram] Rate limited — waiting {wait}s...")
            time.sleep(wait + 1)
            continue

        if resp.status_code == 400 and parse_mode:
            # MarkdownV2 escaping issue — resend as plain text
            print("[telegram] Parse error, resending without MarkdownV2")
            return _send(chat_id, text, parse_mode=None, max_attempts=1)

        print(f"[telegram] Failed: {resp.text}")
        return False

    print(f"[telegram] Gave up after {max_attempts} attempts")
    return False


def notify_jobs(pending: list) -> int:
    """Send one message per pending job row and mark it notified."""
    sent = 0
    for job in pending:
        try:
            extra = json.loads(job["extra"] or "{}")
        except (ValueError, TypeError):
            extra = {}

        mgr = ""
        if extra.get("hiring_manager_name"):
            mgr = (
                f"\n👤 *{escape_md(extra['hiring_manager_name'])}*"
                f" — {escape_md(extra.get('hiring_manager_role') or '')}"
            )

        text = (
            f"🆕 *{escape_md(job['title'])}*\n"
            f"🏢 {escape_md(job['company'])}\n"
            f"🕐 Posted {escape_md(job['posted_at'])}"
            f"{mgr}\n\n"
            f"[View]({job['link']})"
        )

        if _send(TELEGRAM_CHAT_ID, text):
            db.mark_notified(job["id"])
            sent += 1
            time.sleep(0.3)

    return sent


def notify_failure(subject: str, detail: str = "", snapshot: str | None = None,
                   hint: str | None = None) -> bool:
    """Send an alert to the failure channel."""
    if not TELEGRAM_FAILURE_CHAT_ID:
        print("[telegram] No failure chat id configured — alert printed only.")
        print(f"[alert] {subject}: {detail}")
        return False

    text = f"🚨 *{escape_md(subject)}*\n\n{escape_md(detail)}"
    if snapshot:
        text += f"\n\n📎 Snapshot: `{escape_md(snapshot)}`"
    if hint:
        text += f"\n\n🔧 {escape_md(hint)}"
    return _send(TELEGRAM_FAILURE_CHAT_ID, text)

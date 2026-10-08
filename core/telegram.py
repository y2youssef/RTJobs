"""Telegram messaging: job notifications (jobs channel) + failure alerts
(dedicated failure channel)."""

import logging
import json
import re
import time
from urllib.parse import quote, urlsplit

import requests

from config import (TELEGRAM_CHAT_ID, TELEGRAM_FAILURE_CHAT_ID, TELEGRAM_TOKEN, TELEGRAM_API_BASE_URL,
                    ENRICHMENT_ENABLED, CLASSIFIED_DELIVERY_ENABLED, NOTIFY_PER_CHANNEL_LIMIT)
from core import clock, db

logger = logging.getLogger(__name__)

_API = f"{TELEGRAM_API_BASE_URL}/bot{TELEGRAM_TOKEN}"
_SESSION = requests.Session()
_LAST_SEND: dict[str, float] = {}
_RETRY_AFTER: dict[str, float] = {}


_MD_SPECIAL = r"_*\[\]()~`>#+\-=|{}.!\\"


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(f"([{_MD_SPECIAL}])", r"\\\1", text or "")


def _unescape_md(text: str) -> str:
    """Best-effort plain rendering of a MarkdownV2 message (fallback only)."""
    return re.sub(f"\\\\([{_MD_SPECIAL}])", r"\1", text or "")


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _send(
    chat_id: str,
    text: str,
    parse_mode: str = "MarkdownV2",
    max_attempts: int = 3,
    plain: str | None = None,
) -> bool:
    """Send one message; on a MarkdownV2 parse error resend `plain` (the
    readable unformatted version) instead of the escaped markup."""
    for attempt in range(max_attempts):
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            resp = _SESSION.post(
                f"{_API}/sendMessage",
                json=payload,
                timeout=10,
            )
        except requests.RequestException as e:
            logger.warning("[telegram] Request error (attempt %s): %s", attempt + 1, type(e).__name__)
            time.sleep(2)
            continue

        if resp.ok:
            return True

        if resp.status_code == 429:
            try:
                wait = max(1, int(resp.json().get("parameters", {}).get("retry_after", 30)))
            except (ValueError, TypeError, AttributeError):
                wait = 30
            _RETRY_AFTER[str(chat_id)] = time.time() + wait + 1
            logger.warning(f"[telegram] Rate limited — waiting {wait}s...")
            if wait > 30:
                return False
            time.sleep(wait + 1)
            continue

        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 400 and parse_mode:
            # MarkdownV2 escaping issue — resend as plain text
            logger.warning("[telegram] Parse error, resending without MarkdownV2")
            return _send(chat_id, plain if plain is not None else _unescape_md(text),
                         parse_mode=None, max_attempts=1)

        logger.error(f"[telegram] Failed: {resp.text}")
        return False

    logger.error(f"[telegram] Gave up after {max_attempts} attempts")
    return False


def notify_jobs(pending: list, *, stop=None, progress=None) -> int:
    """Send one message per pending job row and mark it notified."""
    if ENRICHMENT_ENABLED and not CLASSIFIED_DELIVERY_ENABLED:
        logger.info("[telegram] Classified delivery is disabled; leaving jobs pending.")
        return 0
    sent = 0
    counts: dict[str, int] = {}
    channels = {}
    if ENRICHMENT_ENABLED:
        from core.classify import load_channels
        channels = load_channels(require_complete=True)
    for row in pending:
        if stop is not None and stop.is_set():
            break
        if progress is not None:
            progress()
        job = dict(row)
        chat_id = destination_for(job, channels)
        if counts.get(chat_id, 0) >= NOTIFY_PER_CHANNEL_LIMIT:
            continue
        counts[chat_id] = counts.get(chat_id, 0) + 1
        cooldown = _RETRY_AFTER.get(chat_id, 0) - time.time()
        if cooldown > 0:
            # Waiting out Telegram's rate limit is not a delivery failure:
            # no attempt counted, no exponential backoff, no retries alert.
            db.defer_notification(job["id"], minimum_delay=cooldown, count_attempt=False)
            continue
        try:
            extra = json.loads(job["extra"] or "{}")
        except (ValueError, TypeError):
            extra = {}
        if not isinstance(extra, dict):
            extra = {}

        flag = "🇪🇬 " if job.get("country") == "EG" and ENRICHMENT_ENABLED else ""
        title = str(job.get("title") or "Job posting")[:250]
        company = str(job.get("company") or "")[:200]
        posted = str(job.get("posted_at") or "")[:50]
        manager = str(extra.get("hiring_manager_name") or "")[:150]
        role = str(extra.get("hiring_manager_role") or "")[:300]
        mgr = f"\n👤 *{escape_md(manager)}* — {escape_md(role)}" if manager else ""
        reposted = str(job.get("reposted_at") or "")[:50]
        if reposted:
            # Same board job ID refreshed by the employer (core/db.record_listings).
            first_seen = clock.to_local(job.get("scraped_at"))[:10]
            icon, when = "🔁", f"Reposted {reposted} · first seen {first_seen}"
        else:
            icon, when = "🆕", f"Posted {posted}"
        text = (f"{flag}{icon} *{escape_md(title)}*\n🏢 {escape_md(company)}\n"
                f"🕐 {escape_md(when)}{mgr}")
        plain = (f"{flag}{icon} {title}\n🏢 {company}\n🕐 {when}"
                 + (f"\n👤 {manager} — {role}" if manager else ""))
        link = job.get("link") or ""
        if urlsplit(link).scheme in ("http", "https"):
            safe_link = quote(link, safe="/:?&=%#@+;,-._~")
            text += "\n\n[View](" + safe_link + ")"
            plain += "\n\n" + safe_link

        db.set_destination(job["id"], chat_id)
        time.sleep(max(0, _LAST_SEND.get(chat_id, 0) + 1.05 - time.monotonic()))
        ok = _send(chat_id, text, plain=plain)
        _LAST_SEND[chat_id] = time.monotonic()
        if ok:
            db.mark_notified(job["id"])
            sent += 1
            time.sleep(0.05)
        else:
            rate_limited = _RETRY_AFTER.get(chat_id, 0) - time.time()
            if rate_limited > 0:
                db.defer_notification(job["id"], minimum_delay=rate_limited, count_attempt=False)
            else:
                db.defer_notification(job["id"])

    return sent


def destination_for(job: dict, channels: dict) -> str:
    """Freeze delivery destination on first attempt; preserve legacy backlog."""
    if job.get("destination_chat_id"):
        return str(job["destination_chat_id"])
    if ENRICHMENT_ENABLED and job.get("job_family"):
        return channels.get(job["job_family"]) or channels.get("other") or TELEGRAM_CHAT_ID
    return TELEGRAM_CHAT_ID


def notify_failure(subject: str, detail: str = "", snapshot: str | None = None,
                   hint: str | None = None) -> bool:
    """Send an alert to the failure channel."""
    if not TELEGRAM_FAILURE_CHAT_ID:
        logger.warning("[telegram] No failure chat id configured — alert printed only.")
        logger.info(f"[alert] {subject}: {detail}")
        return False

    # Telegram rejects messages over 4096 characters, and exception text
    # (e.g. Playwright call logs) can be far longer: clip each part so the
    # alert always fits instead of failing both send attempts.
    subject, detail = _clip(subject, 200), _clip(detail, 3000)
    snapshot, hint = _clip(snapshot, 200), _clip(hint, 400)
    text = f"🚨 *{escape_md(subject)}*\n\n{escape_md(detail)}"
    plain = f"🚨 {subject}\n\n{detail}"
    if snapshot:
        text += f"\n\n📎 Snapshot: `{escape_md(snapshot)}`"
        plain += f"\n\n📎 Snapshot: {snapshot}"
    if hint:
        text += f"\n\n🔧 {escape_md(hint)}"
        plain += f"\n\n🔧 {hint}"
    return _send(TELEGRAM_FAILURE_CHAT_ID, text, plain=plain)


# ---------------------------------------------------------------------------
# Login-code round-trip (Telegram-assisted auth, e.g. Indeed email codes).
#
# The bot cannot receive webhooks from inside the cron container, so inbound
# codes are collected with getUpdates long-polling instead: ONE blocking call
# (timeout=30) that returns the instant a message arrives — far fewer requests
# than a poll+sleep loop. `allowed_updates` is restricted to message +
# channel_post (user replies in the channel arrive as channel_post).
# The offset is persisted in login_state so each update is consumed once, and
# a `min_update_id` window ignores backlog from before the alert went out.
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_UPDATE_OFFSET_KEY = "telegram_update_offset"


def _api(method: str, payload: dict, timeout: int = 15) -> dict | None:
    """POST to the Bot API; return the decoded JSON envelope or None."""
    try:
        resp = _SESSION.post(f"{_API}/{method}", json=payload, timeout=timeout)
    except requests.RequestException as e:
        logger.warning("[telegram] API error (%s): %s", method, type(e).__name__)
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning(f"[telegram] Non-JSON response ({method}): {resp.status_code}")
        return None
    if not data.get("ok"):
        logger.warning(f"[telegram] API failed ({method}): {data}")
        return None
    return data


def get_bot_username() -> str:
    """Bot's @username (for 'DM the code to @bot' instructions). Rare path."""
    data = _api("getMe", {}, timeout=10)
    try:
        return (data or {}).get("result", {}).get("username", "")
    except AttributeError:
        return ""


def send_login_prompt(chat_id: str, text: str) -> int | None:
    """Send plain-text with ForceReply; return the sent message_id so the
    reply carrying the code can be matched to this prompt.

    ForceReply is rejected in channels ("inline keyboard expected") — fall
    back to a plain message there. The prompt tells the user to DM the code
    to the bot, which works in both cases (plain 6-digit DMs are accepted
    as fallback by poll_login_code)."""
    data = _api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {"force_reply": True, "selective": True},
        },
        timeout=15,
    )
    if data is None:
        data = _api(
            "sendMessage", {"chat_id": chat_id, "text": text}, timeout=15
        )
    try:
        return (data or {}).get("result", {}).get("message_id")
    except AttributeError:
        return None


def _get_update_offset() -> int:
    try:
        return int(db.get_state(_UPDATE_OFFSET_KEY, "0"))
    except (ValueError, TypeError):
        return 0


def _set_update_offset(value: int) -> None:
    db.set_state(_UPDATE_OFFSET_KEY, int(value))


def latest_update_id() -> int:
    """Newest queued update_id WITHOUT consuming anything (a negative offset
    never confirms updates). Used to open the code window at alert time."""
    data = _api("getUpdates", {"offset": -1, "limit": 1, "timeout": 0}, timeout=15)
    updates = (data or {}).get("result", []) if data else []
    return max([u.get("update_id", 0) for u in updates] + [0])


def poll_login_code(min_update_id: int = 0, reply_to_msg_id: int | None = None,
                    timeout: int = 30) -> str | None:
    """Long-poll for a 6-digit login code DM'd to the bot.

    Only updates with update_id > min_update_id are eligible (backlog from
    before the alert is ignored). A reply to the ForceReply prompt wins;
    any other 6-digit message is accepted as fallback (the prompt tells the
    user to reply, but plain messages work too). The offset is advanced past
    everything seen so each update is consumed once. Never raises.
    """
    data = _api(
        "getUpdates",
        {
            "offset": _get_update_offset() + 1,
            "limit": 10,
            "timeout": timeout,
            # NOTE: the failure channel is channel-style, so user replies
            # there arrive as channel_post (the bot is admin, so they are
            # delivered). Both kinds are scanned; the reply-to-prompt match
            # binds the code to our alert either way.
            "allowed_updates": ["message", "channel_post"],
        },
        timeout=timeout + 15,
    )
    updates = (data or {}).get("result", []) if data else []
    if updates:
        _set_update_offset(max(u.get("update_id", 0) for u in updates))

    fallback = None
    for u in updates:
        if u.get("update_id", 0) <= min_update_id:
            continue
        msg = u.get("message") or u.get("channel_post") or {}
        m = _CODE_RE.search(msg.get("text") or "")
        if not m:
            continue
        if reply_to_msg_id and (msg.get("reply_to_message") or {}).get(
            "message_id"
        ) == reply_to_msg_id:
            logger.info(
                "[telegram] Login code received"
                f" (reply from chat {msg.get('chat', {}).get('id')})"
            )
            return m.group(1)
        fallback = fallback or m.group(1)
    if fallback:
        logger.info("[telegram] Login code received (plain message)")
    return fallback

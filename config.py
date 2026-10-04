"""
config.py
Single source of truth. All paths and credentials imported from here.
"""

import math
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LINKEDIN_PROFILE_DIR = os.path.abspath(
    os.environ.get("LINKEDIN_PROFILE_DIR", "./chromeprofile")
)
DATA_DIR = os.environ.get("DATA_DIR", ".")
MARKUP_DIR = os.environ.get("MARKUP_DIR", os.path.join(DATA_DIR, "markup"))

DB_PATH = os.path.join(DATA_DIR, "rtjobs.db")

# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"

# Remote debugging port so you can attach to the live headful browser
# (chrome://inspect or any CDP client). Docker maps it to localhost
# via host networking (see docker-compose.yaml).
CHROME_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))

# Kill stray chrome processes before starting (container-only safety net)
KILL_CHROME_ON_START = os.environ.get("KILL_CHROME_ON_START", "false").lower() == "true"

# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------
LINKEDIN_ENABLED = os.environ.get("LINKEDIN_ENABLED", "true").lower() == "true"
LINKEDIN_SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?distance=25&geoId=106155005&keywords=&origin=JOB_SEARCH_PAGE_JOB_FILTER"
    "&refresh=true&sortBy=DD"
)
# Landing page for login: already-logged-in sessions get redirected to the
# feed; logged-out ones get the credential form directly.
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_EMAIL = os.environ.get("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.environ.get("LINKEDIN_PASSWORD", "")
LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS = int(os.environ.get("LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS", "3"))
if not 1 <= LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS <= 30:
    raise ValueError("LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS must be between 1 and 30")

# One bounded recovery navigation after the search fails to render. This is
# additional to scrapling's navigation retries; 0 disables the extra attempt.
LINKEDIN_SEARCH_RECOVERY_ATTEMPTS = int(os.environ.get("LINKEDIN_SEARCH_RECOVERY_ATTEMPTS", "1"))
LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS = int(os.environ.get("LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS", "30"))
LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS = int(os.environ.get("LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS", "20"))
if not 0 <= LINKEDIN_SEARCH_RECOVERY_ATTEMPTS <= 2:
    raise ValueError("LINKEDIN_SEARCH_RECOVERY_ATTEMPTS must be between 0 and 2")
if not 1 <= LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS <= 60:
    raise ValueError("LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS must be between 1 and 60")
if not 1 <= LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS <= 60:
    raise ValueError("LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS must be between 1 and 60")

# How long (seconds) to keep the browser open waiting for a manual
# checkpoint/2FA solve via CDP before aborting the run.
CHECKPOINT_WAIT_SECONDS = int(os.environ.get("CHECKPOINT_WAIT_SECONDS", "600"))

# ---------------------------------------------------------------------------
# Wuzzuf
# ---------------------------------------------------------------------------
WUZZUF_ENABLED = os.environ.get("WUZZUF_ENABLED", "true").lower() == "true"
WUZZUF_SEARCH_URL = os.environ.get("WUZZUF_SEARCH_URL", "https://wuzzuf.net/search/jobs?q=&start=0")
WUZZUF_PROFILE_DIR = os.path.abspath(
    os.environ.get("WUZZUF_PROFILE_DIR", "./wuzzufprofile")
)

# ---------------------------------------------------------------------------
# Indeed
# ---------------------------------------------------------------------------
# Disabled by default until the board is verified live (see INDEED.md).
INDEED_ENABLED = os.environ.get("INDEED_ENABLED", "false").lower() == "true"
INDEED_SEARCH_URL = os.environ.get(
    "INDEED_SEARCH_URL",
    "https://eg.indeed.com/jobs?q=&l=%D9%85%D8%B5%D8%B1&radius=100&sort=date",
)
# Login-gated since ~Oct 2026: anonymous sessions 403-redirect to
# /account/login?branding=login-required&from=bot-detection-anonymous.
# Flow is email -> Continue -> "Sign in with a code instead" (code emailed,
# user DMs it back to the bot — see boards/indeed/login.py). One-time per
# session: cookies persist in the profile volume like LinkedIn.
INDEED_LOGIN_URL = "https://eg.indeed.com/account/login?branding=login-required"
INDEED_EMAIL = os.environ.get("INDEED_EMAIL", "")
# How long (seconds) a run holds the browser open waiting for the user to
# DM the emailed code to the bot before giving up (next tick retries).
INDEED_CODE_WAIT_SECONDS = int(os.environ.get("INDEED_CODE_WAIT_SECONDS", "600"))
INDEED_PROFILE_DIR = os.path.abspath(
    os.environ.get("INDEED_PROFILE_DIR", "./indeedprofile")
)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_FAILURE_CHAT_ID = os.environ.get(
    "TELEGRAM_FAILURE_CHAT_ID", os.environ.get("TELEGRAM_TEST_ID", "")
)

# Validate required env vars at import time with a friendly error (instead
# of a bare KeyError). Collect all missing keys before raising.
_missing = [k for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
}.items() if not v]
# Failure channel is optional, but warn if neither is set (alerts silently dropped).
if _missing:
    raise RuntimeError(
        f"Missing required env vars: {', '.join(_missing)}. "
        "Set them in .env (see README §1)."
    )
del _missing

# Optional dead-man healthcheck (see B4 / README).
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

# ---------------------------------------------------------------------------
# Login retries / cooldown
# ---------------------------------------------------------------------------
MAX_LOGIN_RETRIES = int(os.environ.get("MAX_LOGIN_RETRIES", "3"))
# Cooldown after the 1st, 2nd, 3rd+ consecutive failure.
LOGIN_COOLDOWN_SECONDS = [5 * 60, 15 * 60, 30 * 60]

# ---------------------------------------------------------------------------
# Markup snapshots
# ---------------------------------------------------------------------------
MAX_SNAPSHOTS_PER_KIND = int(os.environ.get("MAX_SNAPSHOTS_PER_KIND", "20"))

# Enrichment is staged separately from scraping. Enable after reviewing a
# dry run; the existing single-channel delivery remains the default.
ENRICHMENT_ENABLED = os.environ.get("ENRICHMENT_ENABLED", "false").lower() == "true"
CLASSIFIED_DELIVERY_ENABLED = os.environ.get("CLASSIFIED_DELIVERY_ENABLED", "false").lower() == "true"
# A semantic schema marker prevents historical industry results being delivered
# by the new family router. Content-cache keys also include prompt/schema hashes.
ENRICHMENT_SCHEMA_VERSION = "job_family_v2"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "openai/gpt-6-luna")
CLASSIFIER_TIMEOUT_SECONDS = int(os.environ.get("CLASSIFIER_TIMEOUT_SECONDS", "45"))
CLASSIFIER_MAX_OUTPUT_TOKENS = int(os.environ.get("CLASSIFIER_MAX_OUTPUT_TOKENS", "2200"))
CLASSIFIER_MAX_INPUT_CHARS = int(os.environ.get("CLASSIFIER_MAX_INPUT_CHARS", "24000"))
CLASSIFIER_DAILY_BUDGET_USD = float(os.environ.get("CLASSIFIER_DAILY_BUDGET_USD", "1"))
CLASSIFIER_MAX_ATTEMPTS = int(os.environ.get("CLASSIFIER_MAX_ATTEMPTS", "3"))
ENRICHMENT_BATCH_SIZE = int(os.environ.get("ENRICHMENT_BATCH_SIZE", "25"))
ENRICHMENT_POLL_SECONDS = int(os.environ.get("ENRICHMENT_POLL_SECONDS", "30"))
NOTIFY_BATCH_SIZE = int(os.environ.get("NOTIFY_BATCH_SIZE", "50"))
NOTIFY_PER_CHANNEL_LIMIT = int(os.environ.get("NOTIFY_PER_CHANNEL_LIMIT", "20"))
TELEGRAM_CHANNELS_JSON = os.environ.get("TELEGRAM_CHANNELS_JSON", "").strip()

# Reject invalid bounds before a worker can issue paid requests or busy-loop.
for _name in ("CLASSIFIER_TIMEOUT_SECONDS", "CLASSIFIER_MAX_OUTPUT_TOKENS",
              "CLASSIFIER_MAX_INPUT_CHARS", "CLASSIFIER_MAX_ATTEMPTS",
              "ENRICHMENT_BATCH_SIZE", "ENRICHMENT_POLL_SECONDS",
              "NOTIFY_BATCH_SIZE", "NOTIFY_PER_CHANNEL_LIMIT"):
    if globals()[_name] <= 0:
        raise ValueError(f"{_name} must be positive")
if not math.isfinite(CLASSIFIER_DAILY_BUDGET_USD) or CLASSIFIER_DAILY_BUDGET_USD < 0:
    raise ValueError("CLASSIFIER_DAILY_BUDGET_USD must be finite and nonnegative")
del _name

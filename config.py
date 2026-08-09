"""
config.py
Single source of truth. All paths and credentials imported from here.
"""

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
# (chrome://inspect or any CDP client). Docker maps it to localhost.
CHROME_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
CHROME_ARGS = [f"--remote-debugging-port={CHROME_DEBUG_PORT}"]

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

# How long (seconds) to keep the browser open waiting for a manual
# checkpoint/2FA solve via CDP before aborting the run.
CHECKPOINT_WAIT_SECONDS = int(os.environ.get("CHECKPOINT_WAIT_SECONDS", "600"))

# ---------------------------------------------------------------------------
# Wuzzuf
# ---------------------------------------------------------------------------
WUZZUF_SEARCH_URL = os.environ.get("WUZZUF_SEARCH_URL", "https://wuzzuf.net/search/jobs?q=&start=0")
WUZZUF_PROFILE_DIR = os.path.abspath(
    os.environ.get("WUZZUF_PROFILE_DIR", "./wuzzufprofile")
)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_FAILURE_CHAT_ID = os.environ.get(
    "TELEGRAM_FAILURE_CHAT_ID", os.environ.get("TELEGRAM_TEST_ID", "")
)

# ---------------------------------------------------------------------------
# Login retries / cooldown
# ---------------------------------------------------------------------------
MAX_LOGIN_RETRIES = int(os.environ.get("MAX_LOGIN_RETRIES", "3"))
# Cooldown after the 1st, 2nd, 3rd+ consecutive failure.
LOGIN_COOLDOWN_SECONDS = [5 * 60, 15 * 60, 30 * 60]

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
MAX_JOBS = int(os.environ.get("MAX_JOBS", "10000"))

# ---------------------------------------------------------------------------
# Markup snapshots
# ---------------------------------------------------------------------------
MAX_SNAPSHOTS_PER_KIND = int(os.environ.get("MAX_SNAPSHOTS_PER_KIND", "20"))

"""RTJobs orchestrator: run every enabled job board, persist jobs, notify."""

import logging
import signal
import sys

from boards.linkedin import LinkedInBoard
from boards.wuzzuf import WuzzufBoard
from boards.indeed import IndeedBoard
from core import db, login_state
from core.log import setup_logging

logger = logging.getLogger(__name__)

_shutting_down = False


def _handle_term(signum, _frame):
    global _shutting_down
    if _shutting_down:
        logger.warning("Second signal %s, forcing exit.", signum)
        sys.exit(1)
    _shutting_down = True
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.warning("Received %s (%s) — shutting down gracefully...", sig_name, signum)
    # Raise to unwind the current board's `try/finally` so `stop_chrome`
    # and `db.finish_run` still execute.
    raise SystemExit(0)


def _install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_term)
        except (ValueError, OSError):
            # Not in main thread (e.g. pytest) — ignore.
            pass

BOARDS = [
    LinkedInBoard,
    WuzzufBoard,
    IndeedBoard,
]


def main() -> int:
    setup_logging()
    _install_signal_handlers()

    # Manual recovery: clear the login retry/cooldown state.
    #   python main.py --reset-login
    if "--reset-login" in sys.argv:
        login_state.reset_retries()
        logger.info("Login retry state reset — next run will attempt login.")
        return 0

    db.init_db()

    total_new = 0
    for board_cls in BOARDS:
        # Check the class attribute FIRST so a disabled board can never
        # abort startup over a missing selectors.json.
        if not getattr(board_cls, "enabled", True):
            logger.info("Board '%s' is disabled — skipping.", board_cls.name)
            continue

        try:
            board = board_cls()
        except FileNotFoundError as e:
            logger.error("Board config missing: %s", e)
            sys.exit(1)

        logger.info("Running board: %s", board.name)
        try:
            total_new += board.run()
        except SystemExit:
            # SIGTERM/SIGINT bubbled from _handle_term — board's own
            # `finally: stop_chrome` + `finish_run` already ran; stop here
            # so Docker's 10s grace isn't wasted on the next board.
            logger.warning("Interrupted during '%s' — exiting.", board.name)
            raise
        except Exception as e:
            logger.exception("Board '%s' crashed: %s", board.name, e)
            from core import telegram

            telegram.notify_failure(f"Board '{board.name}' crashed", str(e))
            if _shutting_down:
                raise SystemExit(0)

    logger.info("Done. New jobs this run: %s", total_new)

    # Dead-man ping (healthchecks.io / Uptime Kuma). Set HEALTHCHECK_URL in
    # .env to enable; failure to ping never fails the run.
    if not _shutting_down:
        try:
            from config import HEALTHCHECK_URL

            if HEALTHCHECK_URL:
                import requests

                requests.get(HEALTHCHECK_URL, timeout=10)
                logger.info("Healthcheck pinged: %s", HEALTHCHECK_URL[:60])
        except Exception as e:
            logger.warning("Healthcheck ping failed (non-fatal): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""RTJobs orchestrator: run every enabled job board, persist jobs, notify."""

import sys

from boards.linkedin import LinkedInBoard
from boards.wuzzuf import WuzzufBoard
from core import db, login_state

BOARDS = [
    LinkedInBoard,
    WuzzufBoard,
]


def main() -> int:
    # Manual recovery: clear the login retry/cooldown state.
    #   python main.py --reset-login
    if "--reset-login" in sys.argv:
        login_state.reset_retries()
        print("[main] Login retry state reset — next run will attempt login.")
        return 0

    db.init_db()

    total_new = 0
    for board_cls in BOARDS:
        # Check the class attribute FIRST so a disabled board can never
        # abort startup over a missing selectors.json.
        if not getattr(board_cls, "enabled", True):
            print(f"[main] Board '{board_cls.name}' is disabled — skipping.")
            continue

        try:
            board = board_cls()
        except FileNotFoundError as e:
            print(f"[main] Board config missing: {e}")
            sys.exit(1)

        print(f"[main] Running board: {board.name}")
        try:
            total_new += board.run()
        except Exception as e:
            print(f"[main] Board '{board.name}' crashed: {e}")
            from core import telegram

            telegram.notify_failure(f"Board '{board.name}' crashed", str(e))

    print(f"[main] Done. New jobs this run: {total_new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

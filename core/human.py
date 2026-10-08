"""Human-like timing helpers.

Every deliberate human-like sleep is recorded as a latency event so the
pacing cost stays visible next to the stages it slows down.
"""

import random
import time

from core import timing


def human_wait(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    """Sleep a random human-like amount of time (recorded as human_delay)."""
    seconds = random.uniform(min_sec, max_sec)
    timing.record("scraper", "human_delay", seconds,
                  {"range": f"{min_sec}-{max_sec}"})
    time.sleep(seconds)


def type_delay(min_ms: int = 60, max_ms: int = 130) -> int:
    """Random per-keystroke delay (ms) for realistic typing."""
    return random.randint(min_ms, max_ms)

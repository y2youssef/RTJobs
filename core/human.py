"""Human-like timing helpers."""

import random
import time


def human_wait(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    """Sleep a random human-like amount of time."""
    time.sleep(random.uniform(min_sec, max_sec))


def type_delay(min_ms: int = 60, max_ms: int = 130) -> int:
    """Random per-keystroke delay (ms) for realistic typing."""
    return random.randint(min_ms, max_ms)

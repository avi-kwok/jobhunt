"""A hard wall-clock deadline for a scan run.

A laptop that sleeps mid-request can leave an HTTPS socket hung well past httpx's
timeout (timeouts don't tick while the machine is suspended). Because launchd runs
one instance per agent, that hung process blocks every later scheduled scan. This
watchdog force-exits after a deadline so the slot frees and the next run proceeds.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable


def _force_exit(seconds: int) -> None:  # pragma: no cover - process-killing default
    print(
        f"[watchdog] scan exceeded {seconds}s hard deadline (likely a stalled "
        "network request after the machine slept); force-exiting so the next "
        "scheduled run isn't blocked.",
        file=sys.stderr,
        flush=True,
    )
    os._exit(1)


def start_watchdog(
    seconds: int, on_expire: Callable[[], None] | None = None
) -> threading.Timer:
    """Start a daemon timer that fires `on_expire` after `seconds`.

    Returns the Timer; call `.cancel()` on it when the run finishes normally.
    A non-positive `seconds` disables the watchdog (returns a cancelled timer).
    """
    action = on_expire or (lambda: _force_exit(seconds))
    timer = threading.Timer(max(0, seconds), action)
    timer.daemon = True
    if seconds and seconds > 0:
        timer.start()
    return timer

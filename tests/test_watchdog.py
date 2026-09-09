"""Tests for the per-run watchdog timer."""
from __future__ import annotations

import threading

from jobhunt.watchdog import start_watchdog


def test_watchdog_fires_after_delay():
    event = threading.Event()
    start_watchdog(0.05, event.set)
    assert event.wait(1) is True


def test_watchdog_cancel_prevents_fire():
    event = threading.Event()
    t = start_watchdog(0.2, event.set)
    t.cancel()
    assert event.wait(0.4) is False


def test_watchdog_zero_seconds_is_disabled():
    event = threading.Event()
    t = start_watchdog(0, event.set)
    assert event.wait(0.2) is False
    # A disabled watchdog is never started, so cancel is a harmless no-op.
    t.cancel()

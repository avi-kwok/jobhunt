"""Spend + rate safeguards around every paid API call.

This is the control layer that makes uncontrolled or runaway key usage
impossible. Before *each* LLM call the guard enforces, in order:

  1. per-run call cap        — no single `scan` can make more than N calls
  2. daily spend cap         — hard USD ceiling per calendar day (UTC)
  3. monthly spend cap       — hard USD ceiling per calendar month (UTC)
  4. minimum call spacing    — a throttle so calls can't fire back-to-back
  5. consecutive-error break — a circuit breaker that trips after repeated errors

Every call's real token usage is recorded to a local, git-ignored SQLite file
so the caps persist across runs and machines-reboots. If any cap would be
exceeded the guard raises `BudgetExceeded` and the caller stops making calls —
it never silently keeps spending.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

USAGE_DB_PATH = Path(".jobhunt_usage.db")

# USD per 1M tokens (input, output). Keyed by model-id prefix.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}
_DEFAULT_PRICE = (5.0, 25.0)  # conservative fallback: assume the priciest tier


class BudgetConfig(BaseModel):
    """Spend + rate limits. Conservative defaults chosen to preserve the key."""

    daily_usd: float = 0.50
    monthly_usd: float = 5.00
    max_calls_per_run: int = 60
    min_seconds_between_calls: float = 0.4
    max_consecutive_errors: int = 3
    # hard per-call output ceilings (defense-in-depth; call sites also set these)
    haiku_max_tokens: int = 256
    sonnet_max_tokens: int = 4096
    enabled: bool = True


class BudgetExceeded(RuntimeError):
    """Raised when a cap would be violated. The caller must stop calling."""


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of one call from real token counts."""
    price_in, price_out = _DEFAULT_PRICE
    for prefix, price in PRICING.items():
        if model.startswith(prefix):
            price_in, price_out = price
            break
    return input_tokens / 1_000_000 * price_in + output_tokens / 1_000_000 * price_out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_log (
    ts             TEXT,
    model          TEXT,
    kind           TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
"""


class BudgetGuard:
    """Enforces spend + rate caps and records real usage. One per process/run."""

    def __init__(self, config: BudgetConfig | None = None, db_path: Path | str = USAGE_DB_PATH):
        self.config = config or BudgetConfig()
        self.db_path = Path(db_path)
        # shared across overlapping runs: wait on locks instead of erroring, so
        # the daily/monthly spend caps stay consistent across both schedules.
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._calls_this_run = 0
        self._consecutive_errors = 0
        self._last_call_ts = 0.0

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "BudgetGuard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- spend queries ----------------------------------------------------

    def _spent(self, since_key: str, like: str) -> float:
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage_log WHERE {since_key} LIKE ?",
            (like,),
        ).fetchone()
        return float(row["s"])

    def spent_today(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._spent("ts", f"{today}%")

    def spent_this_month(self) -> float:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return self._spent("ts", f"{month}%")

    # ---- gate + record ----------------------------------------------------

    def before_call(self, kind: str = "match") -> None:
        """Raise BudgetExceeded if this call must not proceed; else throttle."""
        if not self.config.enabled:
            return

        if self._consecutive_errors >= self.config.max_consecutive_errors:
            raise BudgetExceeded(
                f"circuit breaker: {self._consecutive_errors} consecutive API errors — "
                "stopping to preserve the key. Re-run later."
            )
        if self._calls_this_run >= self.config.max_calls_per_run:
            raise BudgetExceeded(
                f"per-run cap reached ({self.config.max_calls_per_run} calls). "
                "Raise budget.max_calls_per_run to scan more in one run."
            )
        day = self.spent_today()
        if day >= self.config.daily_usd:
            raise BudgetExceeded(
                f"daily spend cap reached (${day:.4f} >= ${self.config.daily_usd:.2f})."
            )
        month = self.spent_this_month()
        if month >= self.config.monthly_usd:
            raise BudgetExceeded(
                f"monthly spend cap reached (${month:.4f} >= ${self.config.monthly_usd:.2f})."
            )

        # throttle: never fire calls uncontrolled/back-to-back
        elapsed = time.monotonic() - self._last_call_ts
        wait = self.config.min_seconds_between_calls - elapsed
        if self._last_call_ts and wait > 0:
            time.sleep(wait)

    def record(self, model: str, input_tokens: int, output_tokens: int, kind: str = "match") -> float:
        """Persist real usage after a successful call. Returns the call's cost."""
        cost = estimate_cost(model, input_tokens, output_tokens)
        self.conn.execute(
            "INSERT INTO usage_log (ts, model, kind, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                model,
                kind,
                input_tokens,
                output_tokens,
                cost,
            ),
        )
        self.conn.commit()
        self._calls_this_run += 1
        self._consecutive_errors = 0
        self._last_call_ts = time.monotonic()
        return cost

    def record_error(self) -> None:
        """Advance the circuit breaker after a failed call."""
        self._consecutive_errors += 1
        self._last_call_ts = time.monotonic()

    # ---- reporting --------------------------------------------------------

    def status(self) -> dict:
        return {
            "spent_today": round(self.spent_today(), 4),
            "daily_cap": self.config.daily_usd,
            "spent_month": round(self.spent_this_month(), 4),
            "monthly_cap": self.config.monthly_usd,
            "calls_this_run": self._calls_this_run,
            "run_cap": self.config.max_calls_per_run,
        }

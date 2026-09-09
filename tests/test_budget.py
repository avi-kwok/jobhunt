"""Spend + rate guard: caps, circuit breaker, cost accounting."""

import pytest

from jobhunt.budget import BudgetConfig, BudgetExceeded, BudgetGuard, estimate_cost


def test_estimate_cost_haiku():
    # 1M input @ $1 + 1M output @ $5
    assert estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(1.0)
    assert estimate_cost("claude-haiku-4-5-20251001", 0, 1_000_000) == pytest.approx(5.0)


def test_estimate_cost_sonnet():
    assert estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_unknown_model_uses_conservative_default():
    # falls back to the priciest tier so an unknown model can't underestimate
    assert estimate_cost("mystery-model", 1_000_000, 0) == pytest.approx(5.0)


def _guard(tmp_path, **overrides):
    cfg = BudgetConfig(min_seconds_between_calls=0, **overrides)
    return BudgetGuard(cfg, db_path=tmp_path / "usage.db")


def test_per_run_call_cap(tmp_path):
    g = _guard(tmp_path, max_calls_per_run=2)
    for _ in range(2):
        g.before_call()
        g.record("claude-haiku-4-5", 100, 10)
    with pytest.raises(BudgetExceeded, match="per-run cap"):
        g.before_call()
    g.close()


def test_daily_cap(tmp_path):
    g = _guard(tmp_path, daily_usd=0.001)
    g.before_call()
    # one call that costs more than the daily cap
    g.record("claude-sonnet-4-6", 1000, 1000)  # ~$0.018
    with pytest.raises(BudgetExceeded, match="daily spend cap"):
        g.before_call()
    g.close()


def test_monthly_cap(tmp_path):
    g = _guard(tmp_path, monthly_usd=0.001, daily_usd=999)
    g.before_call()
    g.record("claude-sonnet-4-6", 1000, 1000)
    with pytest.raises(BudgetExceeded, match="monthly spend cap"):
        g.before_call()
    g.close()


def test_circuit_breaker(tmp_path):
    g = _guard(tmp_path, max_consecutive_errors=2)
    g.before_call()
    g.record_error()
    g.before_call()
    g.record_error()
    with pytest.raises(BudgetExceeded, match="circuit breaker"):
        g.before_call()
    g.close()


def test_successful_call_resets_error_streak(tmp_path):
    g = _guard(tmp_path, max_consecutive_errors=2)
    g.before_call()
    g.record_error()
    g.before_call()
    g.record("claude-haiku-4-5", 100, 10)  # success resets the streak
    g.before_call()
    g.record_error()
    g.before_call()  # only one error since the reset — must not trip
    g.close()


def test_caps_persist_across_instances(tmp_path):
    db = tmp_path / "usage.db"
    g1 = BudgetGuard(BudgetConfig(min_seconds_between_calls=0, daily_usd=0.001), db_path=db)
    g1.before_call()
    g1.record("claude-sonnet-4-6", 1000, 1000)
    g1.close()
    # a fresh guard (e.g. next scan run) still sees the spend
    g2 = BudgetGuard(BudgetConfig(min_seconds_between_calls=0, daily_usd=0.001), db_path=db)
    with pytest.raises(BudgetExceeded, match="daily spend cap"):
        g2.before_call()
    g2.close()


def test_disabled_guard_never_blocks(tmp_path):
    g = _guard(tmp_path, enabled=False, max_calls_per_run=0)
    g.before_call()  # would raise if enforced
    g.close()

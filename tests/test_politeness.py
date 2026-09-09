"""Politeness: tier-preserving shuffle, config load, 429 Retry-After handling."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from jobhunt.adapters.base import request_with_retry
from jobhunt.cli import _in_quiet_hours, _order_companies
from jobhunt.models import CompanyConfig, PolitenessConfig, ScheduleConfig


def _co(name, prio):
    return CompanyConfig(name=name, priority=prio, ats="greenhouse", slug=name.lower())


def test_shuffle_preserves_tier_order():
    companies = [_co(f"c{i}", p) for p in (1, 2, 3) for i in range(5)]
    pol = PolitenessConfig(shuffle_within_tier=True)
    for _ in range(10):
        ordered = _order_companies(companies, pol)
        prios = [c.priority for c in ordered]
        # tiers must remain non-decreasing even though membership within is shuffled
        assert prios == sorted(prios)
        assert len(ordered) == len(companies)


def test_no_shuffle_is_deterministic_priority_sort():
    companies = [_co("b", 2), _co("a", 1), _co("c", 1)]
    pol = PolitenessConfig(shuffle_within_tier=False)
    ordered = _order_companies(companies, pol)
    assert [c.priority for c in ordered] == [1, 1, 2]


def test_config_loads_politeness_defaults():
    from pathlib import Path

    from jobhunt.config import load_config

    cfg = load_config(Path("config.yaml"))
    assert cfg.politeness is not None
    assert cfg.politeness.company_delay_max >= cfg.politeness.company_delay_min


def _et(h, m):
    # a datetime at h:m US Eastern
    return datetime(2026, 7, 3, h, m, tzinfo=ZoneInfo("America/New_York"))


def test_quiet_hours_wrap_past_midnight():
    sched = ScheduleConfig(quiet_start="21:00", quiet_end="02:00")
    assert _in_quiet_hours(sched, _et(21, 0)) is True   # start boundary
    assert _in_quiet_hours(sched, _et(23, 30)) is True
    assert _in_quiet_hours(sched, _et(1, 59)) is True
    assert _in_quiet_hours(sched, _et(2, 0)) is False    # end boundary → resumes
    assert _in_quiet_hours(sched, _et(12, 0)) is False   # midday
    assert _in_quiet_hours(sched, _et(20, 59)) is False  # just before quiet


def test_quiet_hours_disabled():
    sched = ScheduleConfig(enabled=False)
    assert _in_quiet_hours(sched, _et(23, 0)) is False


def test_quiet_hours_timezone_conversion():
    # 22:00 US Eastern is inside the window even when the passed dt is in UTC
    sched = ScheduleConfig()
    utc_2200_eastern = datetime(2026, 7, 4, 2, 0, tzinfo=ZoneInfo("UTC"))  # 22:00 EDT
    assert _in_quiet_hours(sched, utc_2200_eastern) is True


@respx.mock
def test_429_retry_after_is_honored_then_succeeds():
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"jobs": []})

    respx.get("https://x.test/api").mock(side_effect=responder)
    resp = request_with_retry(httpx.Client(), "GET", "https://x.test/api", backoff=0)
    assert resp.status_code == 200
    assert calls["n"] == 2  # retried once after the 429

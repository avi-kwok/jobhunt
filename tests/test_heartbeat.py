"""Heartbeat: health verdict, 24h DB counts, and --no-notify (no network)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from typer.testing import CliRunner

from jobhunt import cli
from jobhunt.store import Store

runner = CliRunner()


def _fake_cfg(watchlist: int = 3):
    companies = [SimpleNamespace(name=f"C{i}", ats="greenhouse") for i in range(watchlist)]
    # one unresolved company should NOT count toward the watchlist
    companies.append(SimpleNamespace(name="Unresolved", ats=None))
    return SimpleNamespace(companies=companies)


def _seed_db(path: str) -> None:
    """Seed a DB: 2 matched-recent, 1 matched-old, 2 notified-recent."""
    with Store(path) as store:
        conn = store.conn
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=3)

        def ins(uid, first_seen, matched, notified):
            conn.execute(
                "INSERT INTO seen_jobs (uid, company, title, url, first_seen, "
                "matched, notified) VALUES (?,?,?,?,?,?,?)",
                (uid, "C", "SWE Intern", "u", first_seen.isoformat(), matched, notified),
            )

        ins("m1", now, 1, 1)
        ins("m2", now, 1, 1)
        ins("m_old", old, 1, 1)        # matched but >24h -> excluded from 24h counts
        ins("seen1", now, 0, 0)        # seen, not matched
        conn.commit()


# ---- (b) 24h matched/notified counts --------------------------------------

def test_db_counts_24h_window(tmp_path):
    db = str(tmp_path / "jobhunt.db")
    _seed_db(db)
    counts = cli._heartbeat_db_counts(db)
    assert counts["total"] == 4
    assert counts["matched_24h"] == 2   # m_old excluded
    assert counts["notified_24h"] == 2


# ---- (a) health verdict: fresh vs stale cron.log --------------------------

def _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=None, watchlist=3):
    db = str(tmp_path / "jobhunt.db")
    Store(db).close()  # create empty schema
    logs = tmp_path / "logs"
    logs.mkdir()
    cron_log = logs / "cron.log"
    if cron_age_min is not None:
        cron_log.write_text("fired\n")
        ts = (datetime.now(timezone.utc) - timedelta(minutes=cron_age_min)).timestamp()
        import os

        os.utime(cron_log, (ts, ts))

    monkeypatch.setattr(cli, "CRON_LOG_PATH", cron_log)
    monkeypatch.setattr(cli, "LAST_RUN_PATH", logs / "last_run.json")
    monkeypatch.setattr(cli, "load_config", lambda: _fake_cfg(watchlist))
    return runner.invoke(cli.app, ["heartbeat", "--no-notify", "--db", db])


def test_health_fresh_cron_is_healthy(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=10)
    assert result.exit_code == 0
    assert "healthy" in result.output
    assert "no cron activity" not in result.output


def test_health_stale_cron_is_unhealthy(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=200)
    assert result.exit_code == 0
    assert "no cron activity" in result.output


def test_health_missing_cron_log_is_unhealthy(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=None)
    assert result.exit_code == 0
    assert "no cron.log" in result.output


def test_watchlist_counts_only_resolved(monkeypatch, tmp_path):
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=10, watchlist=5)
    assert result.exit_code == 0
    # 5 resolved shown as the watchlist (the unresolved company is excluded)
    assert "5" in result.output


# ---- (c) --no-notify prints and does NOT hit the webhook ------------------

def test_no_notify_does_not_call_webhook(monkeypatch, tmp_path):
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k):
            called["n"] += 1

        def send_status(self, *a, **k):
            called["n"] += 1

    monkeypatch.setattr(cli, "DiscordNotifier", _Boom)
    result = _run_heartbeat_no_notify(monkeypatch, tmp_path, cron_age_min=10)
    assert result.exit_code == 0
    assert called["n"] == 0  # webhook path never touched


def test_notify_path_calls_send_status(monkeypatch, tmp_path):
    captured = {}

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def send_status(self, title, fields, color):
            captured["title"] = title
            captured["color"] = color
            captured["fields"] = fields

    db = str(tmp_path / "jobhunt.db")
    Store(db).close()
    logs = tmp_path / "logs"
    logs.mkdir()
    cron_log = logs / "cron.log"
    cron_log.write_text("fired\n")
    last_run = logs / "last_run.json"
    last_run.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "companies": 3, "new_matches": 1,
        "spend_today_usd": 0.0, "spend_month_usd": 0.13,
    }))

    monkeypatch.setattr(cli, "CRON_LOG_PATH", cron_log)
    monkeypatch.setattr(cli, "LAST_RUN_PATH", last_run)
    monkeypatch.setattr(cli, "load_config", lambda: _fake_cfg())
    monkeypatch.setattr(cli, "DiscordNotifier", _Fake)

    result = runner.invoke(cli.app, ["heartbeat", "--db", db])
    assert result.exit_code == 0
    assert captured["title"].startswith("🟢")
    assert captured["color"] == 0x2ECC71
    names = {n for n, _ in captured["fields"]}
    assert {"Status", "Watchlist", "Spend"} <= names
    spend = dict(captured["fields"])["Spend"]
    assert spend == "$0.00 today · $0.13 mo"


# ---- send_status no-ops on empty fields -----------------------------------

def test_send_status_noop_on_empty_fields():
    from jobhunt.notifier import DiscordNotifier

    posted = []
    notifier = DiscordNotifier(webhook_url="https://example/webhook")
    notifier._post = lambda embeds: posted.append(embeds)
    notifier.send_status("title", [], 0x000000)
    assert posted == []

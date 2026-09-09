"""SQLite seen-store: dedupe postings and remember match/notify state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job, MatchResult

DEFAULT_DB_PATH = Path("jobhunt.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    uid              TEXT PRIMARY KEY,
    company          TEXT,
    title            TEXT,
    url              TEXT,
    first_seen       TEXT,
    matched          INTEGER DEFAULT 0,
    notified         INTEGER DEFAULT 0,
    score            INTEGER,
    reason           TEXT,
    location_tier    INTEGER,
    company_priority INTEGER
);
"""


class Store:
    """Thin wrapper over a SQLite DB of postings we've already seen."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        # timeout: overlapping scheduled runs (tier-1 + full) share this DB;
        # wait up to 30s for a lock instead of raising "database is locked".
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def filter_new(self, jobs: list[Job]) -> list[Job]:
        """Return only the jobs whose uid is not already in the store."""
        if not jobs:
            return []
        uids = [j.uid for j in jobs]
        placeholders = ",".join("?" * len(uids))
        rows = self.conn.execute(
            f"SELECT uid FROM seen_jobs WHERE uid IN ({placeholders})", uids
        ).fetchall()
        known = {r["uid"] for r in rows}
        return [j for j in jobs if j.uid not in known]

    def record(self, job: Job, match: MatchResult | None) -> None:
        """Insert a newly-seen job with its match verdict (idempotent on uid)."""
        matched = int(bool(match and match.is_match))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_jobs
              (uid, company, title, url, first_seen, matched, notified,
               score, reason, location_tier, company_priority)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                job.uid,
                job.company,
                job.title,
                job.url,
                datetime.now(timezone.utc).isoformat(),
                matched,
                match.score if match else None,
                match.reason if match else None,
                match.location_tier if match else None,
                match.company_priority if match else None,
            ),
        )
        self.conn.commit()

    def mark_notified(self, uids: list[str]) -> None:
        """Flag the given uids as already-notified so we don't re-ping."""
        if not uids:
            return
        self.conn.executemany(
            "UPDATE seen_jobs SET notified = 1 WHERE uid = ?",
            [(u,) for u in uids],
        )
        self.conn.commit()

    def matches_since(self, since: datetime | None = None) -> list[sqlite3.Row]:
        """All matched rows (optionally first-seen on/after `since`), best-sorted."""
        sql = "SELECT * FROM seen_jobs WHERE matched = 1"
        params: list = []
        if since is not None:
            sql += " AND first_seen >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY company_priority ASC, location_tier ASC, score DESC"
        return self.conn.execute(sql, params).fetchall()

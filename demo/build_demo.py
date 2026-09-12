#!/usr/bin/env python3
"""Render a static, Discord-style page of real jobhunt alerts.

Reads the seen-store and emits a single self-contained HTML file — no
dependencies, no build step, no backend. Picks the best-scoring alert per
company so one prolific board can't dominate the sample.

    python demo/build_demo.py --db jobhunt.db --out docs/index.html --limit 30
"""

from __future__ import annotations

import argparse
import html
import sqlite3
from datetime import datetime
from pathlib import Path

SNAPSHOT_DATE = "September 12, 2026"

# Embed accent by score band — mirrors how the Discord notifier colours them.
def accent(score: int) -> str:
    if score >= 90:
        return "#23a55a"
    if score >= 85:
        return "#5865f2"
    if score >= 80:
        return "#eb8c00"
    return "#949ba4"


TIERS = {1: "Vancouver / Toronto", 2: "US metro", 3: "Rest of Canada", 4: "Remote", 5: "Other"}


def fetch(db: Path, limit: int) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY company ORDER BY score DESC, first_seen DESC
            ) rn
            FROM seen_jobs
            WHERE notified = 1 AND score IS NOT NULL
        )
        SELECT company, title, url, score, reason, location_tier, first_seen
        FROM ranked WHERE rn = 1
        ORDER BY first_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    con.close()
    return rows


def stamp(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%b %-d, %Y at %-I:%M %p")


def render(rows: list[sqlite3.Row]) -> str:
    e = html.escape
    cards = []
    for r in rows:
        tier = TIERS.get(r["location_tier"], "—")
        cards.append(f"""
      <article class="msg">
        <img class="avatar" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'><rect width='40' height='40' rx='20' fill='%235865f2'/><text x='20' y='27' font-size='20' text-anchor='middle' fill='white' font-family='sans-serif'>j</text></svg>" alt="">
        <div class="body">
          <div class="meta"><span class="author">jobhunt</span><span class="tag">APP</span>
            <time>{e(stamp(r["first_seen"]))}</time></div>
          <div class="embed" style="border-left-color:{accent(r['score'])}">
            <div class="company">{e(r["company"])}</div>
            <a class="title" href="{e(r["url"])}" target="_blank" rel="noopener noreferrer">{e(r["title"])}</a>
            <p class="reason">{e(r["reason"] or "")}</p>
            <div class="fields">
              <div><span>Match score</span><b>{r["score"]}</b></div>
              <div><span>Location tier</span><b>{e(tier)}</b></div>
            </div>
          </div>
        </div>
      </article>""")

    return f"""<title>jobhunt — live alert demo</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#313338; color:#dbdee1;
         font:15px/1.5 "gg sans","Helvetica Neue",Helvetica,Arial,sans-serif; }}
  a {{ color:#00a8fc; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .app {{ display:flex; height:100vh; }}
  .rail {{ width:72px; background:#1e1f22; display:flex; flex-direction:column;
           align-items:center; padding-top:12px; gap:8px; flex-shrink:0; }}
  .rail .server {{ width:48px; height:48px; border-radius:16px; background:#5865f2;
                   display:grid; place-items:center; font-weight:700; color:#fff; }}
  .sidebar {{ width:240px; background:#2b2d31; padding:16px 8px; flex-shrink:0; }}
  .sidebar h2 {{ font-size:12px; text-transform:uppercase; color:#949ba4;
                 margin:0 0 8px 8px; letter-spacing:.02em; }}
  .chan {{ padding:6px 8px; border-radius:4px; color:#949ba4; }}
  .chan.active {{ background:#404249; color:#fff; }}
  .main {{ flex:1; display:flex; flex-direction:column; min-width:0; }}
  header {{ height:48px; border-bottom:1px solid #1f2023; display:flex; align-items:center;
            padding:0 16px; font-weight:600; gap:8px; flex-shrink:0; }}
  header .hash {{ color:#949ba4; font-size:20px; }}
  .notice {{ margin:16px; padding:12px 14px; border-radius:8px; background:#2b2d31;
             border-left:4px solid #eb8c00; color:#b5bac1; font-size:14px; }}
  .feed {{ overflow-y:auto; padding:0 16px 32px; }}
  .msg {{ display:flex; gap:16px; padding:12px 0; }}
  .avatar {{ width:40px; height:40px; border-radius:50%; flex-shrink:0; }}
  .body {{ min-width:0; flex:1; }}
  .meta {{ display:flex; align-items:baseline; gap:8px; margin-bottom:4px; flex-wrap:wrap; }}
  .author {{ font-weight:600; color:#fff; }}
  .tag {{ background:#5865f2; color:#fff; font-size:10px; font-weight:600;
          padding:1px 4px; border-radius:3px; text-transform:uppercase; }}
  .meta time {{ font-size:12px; color:#949ba4; }}
  .embed {{ background:#2b2d31; border-left:4px solid #5865f2; border-radius:4px;
            padding:12px 16px; max-width:520px; }}
  .company {{ font-size:12px; color:#b5bac1; margin-bottom:2px; }}
  .title {{ font-weight:600; font-size:16px; display:block; margin-bottom:6px; }}
  .reason {{ margin:0 0 10px; font-size:14px; color:#b5bac1; }}
  .fields {{ display:flex; gap:32px; flex-wrap:wrap; }}
  .fields span {{ display:block; font-size:12px; font-weight:600; color:#fff; }}
  .fields b {{ font-weight:400; font-size:14px; color:#b5bac1; }}
  footer {{ padding:16px; color:#949ba4; font-size:13px; border-top:1px solid #1f2023; }}
  @media (max-width:720px) {{ .rail,.sidebar {{ display:none; }} }}
</style>

<div class="app">
  <nav class="rail"><div class="server">j</div></nav>
  <nav class="sidebar">
    <h2>Job alerts</h2>
    <div class="chan active"># internship-alerts</div>
    <div class="chan"># heartbeat</div>
  </nav>
  <div class="main">
    <header><span class="hash">#</span> internship-alerts</header>
    <div class="feed">
      <div class="notice">
        <b>Demo snapshot — last updated {SNAPSHOT_DATE}.</b><br>
        These are real alerts produced by <a href="https://github.com/avi-kwok/jobhunt">jobhunt</a>,
        one per company, newest first. Job postings expire, so some links may no longer resolve.
      </div>
      {"".join(cards)}
    </div>
    <footer>
      Showing {len(rows)} of 136 alerts sent between July 2 and September 9, 2026 &middot;
      one per company so no single board dominates &middot;
      <a href="https://github.com/avi-kwok/jobhunt">source on GitHub</a>
    </footer>
  </div>
</div>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("jobhunt.db"))
    ap.add_argument("--out", type=Path, default=Path("docs/index.html"))
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()

    rows = fetch(a.db, a.limit)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(render(rows), encoding="utf-8")
    print(f"wrote {a.out} ({len(rows)} alerts)")


if __name__ == "__main__":
    main()

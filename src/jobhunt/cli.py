"""jobhunt command-line interface."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from . import adapters  # noqa: F401 - registers adapters
from . import security
from .adapters.base import get_adapter
from .budget import BudgetExceeded, BudgetGuard
from .config import load_config
from .matcher import composite_key, dedupe_by_uid, is_fresh, llm_match, prefilter
from .models import Job, MatchResult
from .notifier import LOCATION_LABELS, PRIORITY_LABELS, DiscordNotifier
from .store import Store
from .watchdog import start_watchdog

app = typer.Typer(
    help="Personal SWE-internship scanner + resume tailor.", no_args_is_help=True
)
console = Console()

load_dotenv()

LOGS_DIR = Path("logs")
LAST_RUN_PATH = LOGS_DIR / "last_run.json"
CRON_LOG_PATH = LOGS_DIR / "cron.log"
# cron.log is appended on EVERY cron fire (incl. quiet-hour skips), so a stale
# mtime is the reliable "the box/scheduler is dead" signal.
CRON_STALE_MINUTES = 90


def _write_last_run_marker(companies: int, new_matches: int, spend: dict) -> None:
    """Write logs/last_run.json describing the just-completed real scan."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "companies": companies,
        "new_matches": new_matches,
        "spend_today_usd": spend.get("spent_today"),
        "spend_month_usd": spend.get("spent_month"),
    }
    LAST_RUN_PATH.write_text(json.dumps(payload))


# ---- init -----------------------------------------------------------------

_CONFIG_STUB = """\
criteria:
  internship_terms: [intern, internship, "co-op", coop]
  role_terms: [software engineer, software developer, swe, sde, full stack, backend, frontend]
  exclude_terms: [senior, staff, principal, manager, director, new grad, phd]
  location_match_required: true
  use_llm_filter: true
  freshness_minutes: 60
budget:
  daily_usd: 0.50
  monthly_usd: 5.00
  max_calls_per_run: 60
  min_seconds_between_calls: 0.4
  max_consecutive_errors: 3
location_tiers:
  tier_1_local: [vancouver, toronto]
  tier_2_us_metros: [san francisco, "new york", seattle, boston]
  tier_3_rest_of_canada: [canada, montreal, ottawa, waterloo]
  tier_4_remote: [remote]
company_priorities:
  1: [Stripe]
companies:
  - name: Stripe
"""

_MASTER_STUB = """\
contact:
  name: "Your Name"
  email: "you@example.com"
summary: >
  One-paragraph summary of who you are.
skills:
  languages: [Python]
  frameworks: []
projects: []
experience: []
"""

_ENV_STUB = (
    "ANTHROPIC_API_KEY=\n"
    "DISCORD_WEBHOOK_URL=\n"
    "# Optional: contact address sent in the scraper User-Agent.\n"
    "JOBHUNT_CONTACT=\n"
)


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        console.print(f"[yellow]exists, skipped[/] {path}")
        return False
    path.write_text(content)
    console.print(f"[green]created[/] {path}")
    return True


@app.command()
def init() -> None:
    """Scaffold config.yaml, .env, master_resume.yaml if missing."""
    _write_if_missing(Path("config.yaml"), _CONFIG_STUB)
    _write_if_missing(Path("master_resume.yaml"), _MASTER_STUB)
    _write_if_missing(Path(".env"), _ENV_STUB)
    _write_if_missing(Path(".env.example"), _ENV_STUB)


# ---- discover -------------------------------------------------------------

@app.command()
def discover(
    name: str = typer.Argument(None, help="Company name to resolve."),
    url: str = typer.Option(None, "--url", help="Careers URL to parse directly."),
    all_: bool = typer.Option(False, "--all", help="Resolve every company in config.yaml."),
    concurrency: int = typer.Option(
        8, "--concurrency", help="Companies resolved in parallel (--all)."
    ),
) -> None:
    """Resolve companies to concrete ATS endpoints."""
    from . import discover as disco

    cfg = load_config()

    if all_:
        console.print(
            f"Resolving {len(cfg.companies)} companies "
            f"({concurrency} in parallel)…"
        )

        def _progress(company, hit):
            status = f"[green]{hit.ats}:{hit.slug or hit.tenant}[/]" if hit else "[red]unresolved[/]"
            console.print(f"  P{company.priority} {company.name} -> {status}")

        resolved, unresolved = disco.discover_all(
            cfg.companies, progress=_progress, concurrency=concurrency
        )
        console.print(
            f"\n[green]{len(resolved)} resolved[/] -> companies.resolved.yaml, "
            f"[yellow]{len(unresolved)} unresolved[/] -> unresolved.txt"
        )
        return

    if not name:
        raise typer.BadParameter("provide a NAME, or use --all")

    result = disco.resolve(name, url=url)
    if result is None:
        console.print(f"[red]Could not resolve {name}[/]. Try passing --url <careers URL>.")
        raise typer.Exit(1)
    result.priority = cfg.company_priority.get(name, 4)
    console.print(f"[green]{name}[/] -> {result.model_dump(exclude_none=True)}")
    console.print("Add this to companies.resolved.yaml, or re-run `discover --all`.")


# ---- scan -----------------------------------------------------------------

def _fetch_company(company, errors: list[str]) -> list[Job]:
    if not company.ats:
        return []
    try:
        adapter = get_adapter(company.ats)
        try:
            return adapter.fetch(company)
        finally:
            adapter.close()
    except Exception as exc:  # noqa: BLE001 - collect, never abort the run
        errors.append(f"{company.name} ({company.ats}): {exc}")
        return []


def _order_companies(companies: list, politeness) -> list:
    """Order by priority tier; optionally shuffle within each tier per run."""
    ordered = sorted(companies, key=lambda c: c.priority)
    if not (politeness and politeness.enabled and politeness.shuffle_within_tier):
        return ordered
    # group consecutive same-priority runs, shuffle each, keep tiers in order
    out: list = []
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j].priority == ordered[i].priority:
            j += 1
        group = ordered[i:j]
        random.shuffle(group)
        out.extend(group)
        i = j
    return out


def _pace(politeness) -> None:
    """Random pause between company fetches to spread load (if enabled)."""
    if politeness and politeness.enabled and politeness.company_delay_max > 0:
        time.sleep(random.uniform(politeness.company_delay_min, politeness.company_delay_max))


def _in_quiet_hours(schedule, now=None) -> bool:
    """Whether the current time falls in the configured nightly quiet window."""
    if not (schedule and schedule.enabled):
        return False
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(schedule.quiet_tz)
    now = (now or datetime.now(tz)).astimezone(tz)
    cur = now.hour * 60 + now.minute
    sh, sm = map(int, schedule.quiet_start.split(":"))
    eh, em = map(int, schedule.quiet_end.split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # window wraps past midnight


@app.command()
def scan(
    max_priority: int = typer.Option(
        None, "--max-priority", help="Only scan companies at tier <= N."
    ),
    no_notify: bool = typer.Option(False, "--no-notify", help="Skip Discord notification."),
    seed: bool = typer.Option(
        False,
        "--seed",
        help="Record current postings as seen without matching, notifying, or "
        "spending. Run once before the first real scan to avoid a flood.",
    ),
    no_jitter: bool = typer.Option(
        False, "--no-jitter", help="Skip the random startup delay (for interactive runs)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Run even during the configured nightly quiet hours."
    ),
    db: str = typer.Option("jobhunt.db", "--db", help="SQLite path."),
) -> None:
    """Full pipeline: fetch -> dedupe -> prefilter -> llm -> record -> notify."""
    from .tailor.master import load_master, profile_summary

    cfg = load_config()

    # Nightly quiet window: scheduled runs self-skip (seed/--force bypass it).
    if not seed and not force and _in_quiet_hours(cfg.schedule):
        s = cfg.schedule
        console.print(
            f"[dim]Quiet hours ({s.quiet_start}–{s.quiet_end} {s.quiet_tz}) — "
            "skipping. Use --force to override.[/]"
        )
        return

    companies_all = _order_companies(cfg.companies, cfg.politeness)
    if max_priority is not None:
        companies_all = [c for c in companies_all if c.priority <= max_priority]
    resolved_all = [c for c in companies_all if c.ats]

    # Seed mode: mark every current posting as seen. No prefilter, no LLM, no
    # notify, no API key, no cost — just teaches the seen-store what exists today
    # so the first real scan only surfaces genuinely new postings.
    if seed:
        errors: list[str] = []
        total = 0
        console.print(f"Seeding from {len(resolved_all)} resolved companies (no API cost)…")
        with Store(db) as store:
            for company in resolved_all:
                jobs = dedupe_by_uid(_fetch_company(company, errors))
                for job in store.filter_new(jobs):
                    store.record(job, None)
                    total += 1
                _pace(cfg.politeness)
        console.print(
            f"[green]Seeded {total} current postings as seen.[/] "
            "Future scans will only notify on new ones."
        )
        if errors:
            console.print(f"[yellow]{len(errors)} company error(s) (skipped).[/]")
        return

    # Security preflight: lock down .env, refuse to run if the key is git-tracked,
    # and fail clearly (not mid-scan) if the LLM filter is on but no key is set.
    if cfg.criteria.use_llm_filter:
        try:
            for note in security.preflight():
                console.print(f"[dim]security: {note}[/]")
        except security.SecurityError as exc:
            console.print(f"[red]Security check failed:[/] {exc}")
            raise typer.Exit(2)
        if security.get_api_key(required=False) is None:
            console.print(
                "[red]use_llm_filter is on but ANTHROPIC_API_KEY is not set.[/] "
                "Add it to .env, or set use_llm_filter: false in config.yaml to "
                "run the scanner free (deterministic prefilter only)."
            )
            raise typer.Exit(2)

    profile = ""
    try:
        profile = profile_summary(load_master())
    except Exception:  # noqa: BLE001 - matcher works without a profile summary
        pass

    companies = companies_all
    resolved = resolved_all

    console.print(
        f"Scanning {len(resolved)} resolved companies "
        f"(of {len(companies)} in scope)…"
    )

    # Hard watchdog: force-exit if this run outlives its deadline (a sleep-stalled
    # network request can hang past httpx's timeout and block the next launchd run).
    _watchdog = start_watchdog(cfg.schedule.max_run_seconds)

    # Startup jitter: de-sync scheduled runs so hosts aren't hit on the exact
    # clock second every time. Skip for interactive runs with --no-jitter.
    pol = cfg.politeness
    if pol and pol.enabled and not no_jitter and pol.startup_jitter_seconds > 0:
        delay = random.uniform(0, pol.startup_jitter_seconds)
        console.print(f"[dim]startup jitter: sleeping {delay:.0f}s[/]")
        time.sleep(delay)

    errors = []
    new_matches: list[tuple[Job, MatchResult]] = []
    budget_hit = False

    with Store(db) as store, BudgetGuard(cfg.budget) as guard:
        for idx, company in enumerate(resolved):
            if budget_hit:
                break
            if idx:
                _pace(cfg.politeness)  # random pause between companies
            jobs = _fetch_company(company, errors)
            if not jobs:
                continue
            jobs = dedupe_by_uid(jobs)  # collapse in-fetch duplicates
            fresh = store.filter_new(jobs)  # drop uids we've already seen
            for job in fresh:
                passed, tier = prefilter(job, cfg.criteria, cfg.location_tiers)
                match: MatchResult | None = None
                # Freshness gate runs before the paid LLM call so stale postings
                # never cost anything. Undated sources pass (trust the seen-store).
                if passed and is_fresh(job, cfg.criteria.freshness_minutes):
                    prio = cfg.company_priority.get(company.name, 4)
                    if cfg.criteria.use_llm_filter:
                        try:
                            match = llm_match(
                                job, profile, tier, prio,
                                guard=guard,
                                max_tokens=cfg.budget.haiku_max_tokens,
                            )
                        except BudgetExceeded as exc:
                            # Stop spending. Leave the remaining new jobs UNrecorded
                            # so the next run (after the cap resets) picks them up.
                            console.print(f"[yellow]Budget guard:[/] {exc}")
                            budget_hit = True
                            break
                    else:
                        match = MatchResult(
                            is_match=True,
                            score=70,
                            reason="passed title/location prefilter",
                            location_tier=tier,
                            company_priority=prio,
                        )
                # Record every processed posting (match may be None: not matched or
                # stale) so its uid is remembered and never re-evaluated.
                store.record(job, match)
                if match and match.is_match:
                    new_matches.append((job, match))

        new_matches.sort(key=lambda pair: composite_key(pair[1]))
        spend = guard.status()

        if new_matches and not no_notify:
            try:
                DiscordNotifier().send(new_matches)
                store.mark_notified([job.uid for job, _ in new_matches])
                console.print(f"[green]Notified {len(new_matches)} matches to Discord.[/]")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]Notify skipped:[/] {security.redact(str(exc))}")

    # Real fetching/notifying is done — stand the watchdog down before the
    # (fast, offline) result printing so it can't fire on a normal run.
    _watchdog.cancel()

    # Liveness marker: record that a real scan completed. A marker-write failure
    # must NEVER break or abort a scan, so the whole thing is best-effort.
    try:
        _write_last_run_marker(
            companies=len(resolved),
            new_matches=len(new_matches),
            spend=spend,
        )
    except Exception:  # noqa: BLE001 - liveness marker is best-effort only
        pass

    _print_matches_table(new_matches, title="New matches this run")

    console.print(
        f"[dim]spend: {spend['calls_this_run']}/{spend['run_cap']} calls this run · "
        f"${spend['spent_today']:.4f}/${spend['daily_cap']:.2f} today · "
        f"${spend['spent_month']:.4f}/${spend['monthly_cap']:.2f} this month[/]"
    )
    if budget_hit:
        console.print(
            "[yellow]Scan stopped early at a spend cap. Unscanned new postings "
            "will be picked up next run (caps reset daily/monthly).[/]"
        )

    if errors:
        console.print(f"\n[yellow]{len(errors)} company error(s):[/]")
        for e in errors:
            console.print(f"  [red]•[/] {e}")


# ---- list-matches ---------------------------------------------------------

@app.command("list-matches")
def list_matches(
    since: str = typer.Option(
        None, "--since", help="Only matches first seen since e.g. '7d' or ISO date."
    ),
    max_priority: int = typer.Option(None, "--max-priority"),
    db: str = typer.Option("jobhunt.db", "--db"),
) -> None:
    """Print stored matches ordered by priority then location tier."""
    since_dt = _parse_since(since)
    with Store(db) as store:
        rows = store.matches_since(since_dt)

    table = Table(title="Stored matches")
    for col in ("Prio", "Company", "Location", "Tier", "Score", "Title"):
        table.add_column(col)
    for r in rows:
        if max_priority is not None and (r["company_priority"] or 4) > max_priority:
            continue
        prio = r["company_priority"] or 4
        tier = r["location_tier"] or 5
        prio_label = PRIORITY_LABELS.get(prio, PRIORITY_LABELS[4])[0]
        table.add_row(
            f"P{prio} {prio_label}",
            r["company"],
            "-",
            LOCATION_LABELS.get(tier, "Other"),
            str(r["score"] or ""),
            f"[link={r['url']}]{r['title']}[/link]",
        )
    console.print(table)


# ---- tailor ---------------------------------------------------------------

@app.command()
def tailor(
    url_or_path: str = typer.Argument(..., help="Job URL, file path, or raw JD text."),
    cover_letter: bool = typer.Option(False, "--cover-letter"),
    company: str = typer.Option(None, "--company"),
    role: str = typer.Option(None, "--role"),
) -> None:
    """Produce a tailored resume, gap report, and optional cover letter."""
    from .tailor.tailor import tailor as run_tailor

    cfg = load_config()

    # Same security preflight + spend guard as the scanner.
    try:
        for note in security.preflight():
            console.print(f"[dim]security: {note}[/]")
    except security.SecurityError as exc:
        console.print(f"[red]Security check failed:[/] {exc}")
        raise typer.Exit(2)
    if security.get_api_key(required=False) is None:
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/] The tailor needs it — add it to .env."
        )
        raise typer.Exit(2)

    console.print("Tailoring (facts limited to master_resume.yaml)…")
    with BudgetGuard(cfg.budget) as guard:
        try:
            written = run_tailor(
                url_or_path,
                cover_letter=cover_letter,
                company=company,
                role=role,
                guard=guard,
                max_tokens=cfg.budget.sonnet_max_tokens,
            )
        except BudgetExceeded as exc:
            console.print(f"[red]Refused to spend:[/] {exc}")
            raise typer.Exit(2)
        spend = guard.status()

    for label, path in written.items():
        console.print(f"[green]{label}[/] -> {path}")
    console.print(
        f"[dim]spend: ${spend['spent_today']:.4f}/${spend['daily_cap']:.2f} today · "
        f"${spend['spent_month']:.4f}/${spend['monthly_cap']:.2f} this month[/]"
    )


# ---- security-check -------------------------------------------------------

@app.command("security-check")
def security_check() -> None:
    """Audit API-key protections: file permissions, git-ignore, tracking, format."""
    # Harden first, then report.
    for note in security.harden_env_permissions():
        console.print(f"[dim]{note}[/]")
    all_ok, checks = security.audit()

    table = Table(title="API key security audit")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for label, ok, detail in checks:
        table.add_row(
            label,
            "[green]PASS[/]" if ok else "[red]FAIL[/]",
            security.redact(detail),
        )
    console.print(table)
    if all_ok:
        console.print("[green]All safeguards in place.[/]")
    else:
        console.print(
            "[yellow]Some checks failed — fix before relying on the key.[/] "
            "Common fixes: `cp .env.example .env` and add the key; "
            "ensure `.env` is listed in .gitignore."
        )
        raise typer.Exit(1)


# ---- budget ---------------------------------------------------------------

@app.command()
def budget() -> None:
    """Show configured spend caps and current usage against them."""
    cfg = load_config()
    with BudgetGuard(cfg.budget) as guard:
        s = guard.status()
    c = cfg.budget
    table = Table(title="Spend safeguards")
    table.add_column("Limit")
    table.add_column("Value")
    table.add_row("Daily cap", f"${c.daily_usd:.2f}  (spent today: ${s['spent_today']:.4f})")
    table.add_row("Monthly cap", f"${c.monthly_usd:.2f}  (spent this month: ${s['spent_month']:.4f})")
    table.add_row("Max calls per run", str(c.max_calls_per_run))
    table.add_row("Min seconds between calls", str(c.min_seconds_between_calls))
    table.add_row("Circuit breaker (consecutive errors)", str(c.max_consecutive_errors))
    table.add_row("Haiku max output tokens", str(c.haiku_max_tokens))
    table.add_row("Sonnet max output tokens", str(c.sonnet_max_tokens))
    table.add_row("Guard enabled", str(c.enabled))
    console.print(table)


# ---- heartbeat ------------------------------------------------------------

def _relative_time(then: datetime | None, now: datetime | None = None) -> str:
    """Human-relative age like '12 min ago' / '3 hr ago' / 'just now'."""
    if then is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = (now - then).total_seconds()
    if secs < 0:
        return "just now"
    mins = int(secs // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day ago" if days == 1 else f"{days} days ago"


def _read_last_run(path: Path | None = None) -> dict | None:
    """Read logs/last_run.json, tolerating a missing or corrupt file."""
    path = path if path is not None else LAST_RUN_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _mtime(path: Path) -> datetime | None:
    """UTC mtime of a file, or None if it's missing/unreadable."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _heartbeat_db_counts(db: str) -> dict:
    """Watchlist-independent DB stats: totals + last-24h matched/notified."""
    with Store(db) as store:
        total = store.conn.execute("SELECT COUNT(*) AS n FROM seen_jobs").fetchone()["n"]
        matched_24h = store.conn.execute(
            "SELECT COUNT(*) AS n FROM seen_jobs "
            "WHERE matched = 1 AND first_seen > datetime('now','-1 day')"
        ).fetchone()["n"]
        notified_24h = store.conn.execute(
            "SELECT COUNT(*) AS n FROM seen_jobs "
            "WHERE notified = 1 AND first_seen > datetime('now','-1 day')"
        ).fetchone()["n"]
    return {"total": total, "matched_24h": matched_24h, "notified_24h": notified_24h}


@app.command()
def heartbeat(
    no_notify: bool = typer.Option(
        False, "--no-notify", help="Print the summary to the console instead of posting."
    ),
    db: str = typer.Option("jobhunt.db", "--db", help="SQLite path."),
) -> None:
    """Post a periodic liveness/status summary to Discord (silence = broken)."""
    cfg = load_config()
    watchlist = len([c for c in cfg.companies if c.ats])
    counts = _heartbeat_db_counts(db)

    last_run = _read_last_run()
    last_scan_ts = None
    if last_run and last_run.get("ts"):
        try:
            last_scan_ts = datetime.fromisoformat(last_run["ts"])
        except ValueError:
            last_scan_ts = None

    cron_mtime = _mtime(CRON_LOG_PATH)
    now = datetime.now(timezone.utc)

    # Health verdict: cron.log fires on EVERY scheduled run, so a fresh mtime is
    # the liveness proof. Missing file -> unknown/unhealthy.
    if cron_mtime is None:
        healthy = False
        status_text = "no cron.log found — scheduler may never have run"
    else:
        age_min = int((now - cron_mtime).total_seconds() // 60)
        healthy = age_min <= CRON_STALE_MINUTES
        status_text = (
            "healthy" if healthy
            else f"no cron activity in {age_min} min — check the box"
        )

    # Spend comes from the last completed scan's marker (falls back to unknown).
    if last_run and last_run.get("spend_today_usd") is not None:
        spend_str = (
            f"${last_run['spend_today_usd']:.2f} today · "
            f"${last_run.get('spend_month_usd', 0.0):.2f} mo"
        )
    else:
        spend_str = "unknown"

    fields = [
        ("Status", status_text),
        ("Last cron fire", _relative_time(cron_mtime, now)),
        ("Last completed scan", _relative_time(last_scan_ts, now)),
        ("Watchlist", str(watchlist)),
        ("New matches (24h)", str(counts["matched_24h"])),
        ("Alerts sent (24h)", str(counts["notified_24h"])),
        ("Spend", spend_str),
    ]

    if healthy:
        title, color = "🟢 jobhunt heartbeat", 0x2ECC71
    else:
        title, color = "🔴 jobhunt — no recent activity", 0xE74C3C

    if no_notify:
        table = Table(title=title)
        table.add_column("Field")
        table.add_column("Value")
        for name, value in fields:
            table.add_row(name, value)
        console.print(table)
        return

    try:
        DiscordNotifier().send_status(title, fields, color)
        console.print(f"[green]Posted heartbeat to Discord[/] ({status_text}).")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Heartbeat not posted:[/] {security.redact(str(exc))}")


# ---- helpers --------------------------------------------------------------

def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    if since.endswith("d") and since[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(days=int(since[:-1]))
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        return None


def _print_matches_table(matches: list[tuple[Job, MatchResult]], title: str) -> None:
    if not matches:
        console.print(f"[dim]{title}: none.[/]")
        return
    table = Table(title=title)
    for col in ("Prio", "Company", "Location", "Tier", "Score", "Posted", "Title"):
        table.add_column(col)
    for job, m in matches:
        prio_label = PRIORITY_LABELS.get(m.company_priority, PRIORITY_LABELS[4])[0]
        posted = (
            job.posted_at.strftime("%Y-%m-%d %H:%M")
            if job.posted_at is not None
            else (job.posted_note or "-")
        )
        table.add_row(
            f"P{m.company_priority} {prio_label}",
            job.company,
            job.location or "-",
            LOCATION_LABELS.get(m.location_tier, "Other"),
            str(m.score),
            posted,
            f"[link={job.url}]{job.title}[/link]",
        )
    console.print(table)


if __name__ == "__main__":
    app()

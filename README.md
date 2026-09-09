# jobhunt

A personal command-line tool that helps me apply for **software-engineering internships / co-ops** faster. Two independent tools share one config:

- **Scanner** — on a schedule, fetch new postings from a watchlist of companies' *own* posting systems (first-party ATS endpoints only), keep the ones matching my criteria, rank them by **company priority → location priority → match score**, and notify me on Discord. Only notifies on postings I haven't seen before.
- **Tailor** — on demand, take one posting + my structured master résumé and produce a tailored résumé, a keyword-gap report, and (optionally) a cover letter — **without ever inventing experience**.

This is a personal tool optimized for low-friction reliability, not scale or polish.

## Hard rules

1. **Never scrapes LinkedIn or Indeed.** Only first-party ATS endpoints (Greenhouse, Lever, Ashby, Workday) and company career sites.
2. Prefers ATS JSON endpoints over HTML scraping. Playwright is an optional last resort.
3. **The tailor never fabricates.** It may only *select, reorder, and lightly rephrase* facts already present in `master_resume.yaml`. It never invents roles, dates, metrics, or skills — this is enforced in the system prompt (`src/jobhunt/tailor/tailor.py`). Any `TODO(...)` placeholder bullets in the master are ignored.
4. Polite to endpoints: descriptive User-Agent, low concurrency, timeouts, retry-with-backoff.
5. No secrets in code. `ANTHROPIC_API_KEY` and `DISCORD_WEBHOOK_URL` come from `.env` (git-ignored). Both are masked by `security.redact()` before anything is printed — a webhook URL is itself a credential, so it never reaches a log even in an HTTP error.
6. **No personal data in the repo.** Your `master_resume.yaml` and the `jobhunt.db` seen-store are git-ignored; the repo ships `master_resume.example.yaml` instead.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ",browser" for the optional Playwright extra
cp .env.example .env             # then fill in ANTHROPIC_API_KEY and DISCORD_WEBHOOK_URL
cp master_resume.example.yaml master_resume.yaml   # then replace with your real facts
```

`.env` also takes an optional `JOBHUNT_CONTACT` — an address included in the
scraper's User-Agent as a courtesy to the sites you poll. Leave it blank and no
contact is sent.

## API key security & spend safeguards

The `ANTHROPIC_API_KEY` is protected by layered safeguards so it can't leak, can't be read by other users, and can't be used uncontrolled:

**Key protection**
- The key is read **only** from `.env` → process environment. It's never written elsewhere, never passed on the command line, and any accidental echo is redacted (`sk-ant-***REDACTED***`).
- `.env` is `git`-ignored and auto-`chmod`ed to **600** (owner read/write only) on every run — no other account on the machine can read it.
- Before any paid call, a preflight **refuses to run if `.env` is tracked by git**, so the key can never be committed.
- Run `jobhunt security-check` any time to audit all of this (key present, format, 600 perms, git-ignored, not tracked).

**Spend & rate control** — every LLM call passes through a guard that enforces, in order:
1. **Per-run call cap** (`max_calls_per_run`, default 60) — no single scan can exceed it.
2. **Daily USD cap** (`daily_usd`, default $0.50) and **monthly USD cap** (`monthly_usd`, default $5.00) — hard ceilings; the run stops when hit.
3. **Throttle** (`min_seconds_between_calls`, default 0.4s) — calls can never fire uncontrolled back-to-back.
4. **Circuit breaker** (`max_consecutive_errors`, default 3) — repeated API errors (e.g. a bad key) stop the run instead of hammering it.
5. **Per-call output caps** (`haiku_max_tokens`, `sonnet_max_tokens`).

Real token cost of every call is logged to a local, git-ignored `.jobhunt_usage.db`, so the daily/monthly caps persist across runs and reboots. When a cap trips mid-scan, the run stops and the un-scanned postings are simply picked up on the next run (caps reset by UTC day/month). Tune every limit in the `budget:` block of `config.yaml`; inspect current usage with `jobhunt budget`.

> Belt-and-suspenders: also set a hard **spend limit in the Anthropic Console** (Billing → limits). The in-app guard protects against runaway loops and bad keys; the Console limit is the ultimate backstop on the account itself.

## Configure

- `config.yaml` — criteria (title term matching), location tiers, company priorities, and the watchlist. **Company priority** is written as grouped lists (`1`=top … `3`=tracking) for readability; the loader inverts them into a `name → tier` dict (unlisted companies default to `4`).
- `master_resume.yaml` — an over-complete store of **real** facts. The richer and more truthful it is, the sharper each tailored résumé. Add more bullets than any one résumé needs. **Git-ignored** (it holds your name, email, links and location); copy `master_resume.example.yaml` to create it.

`config.yaml` ships in the repo — edit it, don't recreate it. `master_resume.yaml` is yours to create from the example (`jobhunt init` also scaffolds a blank one when missing).

## Usage

```bash
jobhunt init                       # scaffold config/.env/master if missing
jobhunt discover --all             # resolve the watchlist -> companies.resolved.yaml
jobhunt discover "Stripe"          # resolve one company by name
jobhunt discover "Acme" --url https://acme.wd5.myworkdayjobs.com/en-US/careers
jobhunt scan                       # full pipeline over all resolved companies
jobhunt scan --max-priority 2      # only tiers 1-2 (top + strong)
jobhunt scan --no-notify           # scan without posting to Discord
jobhunt list-matches --since 7d    # print stored matches, best-sorted
jobhunt tailor <URL|path|text> --company Stripe --role "SWE Intern" --cover-letter
jobhunt security-check             # audit API-key protections
jobhunt budget                     # show spend caps and usage
```

### Ranking

Matches sort by a single composite key over the whole batch:

```
(company_priority, location_tier, -score)   # priority first, then location, then higher score
```

To make **location** dominate instead of company priority, swap the first two elements of the tuple in `matcher.composite_key` — a one-line change. Companies are also *fetched* in ascending-priority order, so an interrupted run still covers the best boards first.

Location tiers (from `config.yaml`): Vancouver/Toronto → US metros → rest of Canada → remote → other.

### Freshness & no duplicates

Two independent guarantees keep notifications recent and unique:

- **No duplicates (across runs).** Every posting has a stable `uid = source:company:external_id`, stored in SQLite with `uid` as PRIMARY KEY. `filter_new()` drops any uid already seen and `record()` is `INSERT OR IGNORE`, so a posting is never notified twice — verified: two consecutive scans give N new, then 0. An in-fetch `dedupe_by_uid()` also collapses pagination overlaps so the same posting can't slip through twice within one scan.
- **Freshness (`freshness_minutes`, default 60).** After a posting passes the title/location prefilter, it must also be recent: its own timestamp must be within the window, *and* it runs before the paid LLM call so stale postings cost nothing. The 60-minute window comfortably exceeds the scan cadence (every 15–30 min), so a job posted between two scans is still caught even if a run is missed. Sources with a reliable timestamp (Lever `createdAt`, Ashby `publishedAt`, Amazon `posted_date`) are filtered precisely; **Greenhouse reports edit-time** (a proxy) and **Workday has no absolute timestamp** — those are always included and rely on the seen-store's "new since last scan" guarantee (your chosen policy). Set `freshness_minutes: null` to disable and use the seen-store alone.

Together: the first scan doesn't flood you (stale postings are recorded-as-seen but not notified), and every later scan surfaces only postings that are both **new to you** and **freshly posted** — each exactly once.

## How discovery works

- **URL given:** the ATS is detected from the host (`greenhouse.io`, `lever.co`, `ashbyhq.com`, `myworkdayjobs.com`) and the slug (or Workday tenant/wd/site) is extracted directly.
- **Name only:** slug candidates are generated and probed against Greenhouse, Lever, and Ashby; the first valid JSON board with jobs wins. Workday usually can't be resolved from a name — those land in `unresolved.txt` with a note to supply a careers URL.

`jobhunt discover --all` writes resolved companies to `companies.resolved.yaml` (preserving each company's priority) and the rest to `unresolved.txt`.

## Scheduling

The simplest option is local `cron` / `launchd`. To run every 15 minutes with cron:

```cron
*/15 * * * * cd /path/to/jobhunt && .venv/bin/jobhunt scan >> scan.log 2>&1
```

Alternatively, the optional GitHub Action at `.github/workflows/scan.yml` runs `jobhunt scan` **every 15 minutes** (and on manual dispatch). It does **not** commit the seen-store back — `jobhunt.db` is your job-search history and stays out of the repo — so each CI run starts fresh and may re-notify on already-seen postings. Local cron/launchd keeps the store on your own machine and is the recommended way to run this. Secrets come from GitHub Actions secrets (`ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`). The Action is optional; local cron is the simpler default.

## Models

- Relevance matching (per new posting): `claude-haiku-4-5-20251001`
- Tailoring + cover letter: `claude-sonnet-4-6`

## Tests

```bash
pytest
```

Adapters and discovery are tested against mocked HTTP responses (no network). The LLM stages are pure/​defensive and don't require an API key to test.

## Project layout

```
src/jobhunt/
  models.py      config.py      store.py       matcher.py
  notifier.py    discover.py    cli.py
  adapters/      greenhouse lever ashby workday amazon
  tailor/        master.py  tailor.py
tests/
```

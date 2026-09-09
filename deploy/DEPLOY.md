# Deploying jobhunt to an always-on Linux box

This runbook moves the jobhunt scanner off your Mac and onto an always-on Linux
machine driven by cron. Do the steps in order.

## 1. Provision a Linux box (Ubuntu 24.04 recommended)

Pick one:

- **Oracle Cloud "Always Free" ARM VM** — $0/month, generous specs. Best value.
- **Small VPS** — Hetzner / DigitalOcean / AWS Lightsail, ~$4–5/month. Simplest.
- **Home Raspberry Pi** — free if you already own one; Raspberry Pi OS works too
  (it's Debian-based, so the setup script's `apt` path handles it).

Whatever you choose, you need **SSH access**: the box's public IP and an SSH key.
Give the machine the username you'll use below (examples use `ubuntu`).

## 2. Copy the project over from the Mac (with secrets + seen-store)

Run this **on the Mac**. It carries `.env` (secrets) and `jobhunt.db` (the
seen-store) so the first scan on the box does NOT flood Discord, while skipping
the Mac-specific virtualenv and caches:

```bash
rsync -av --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  ~/jobhunt/ USER@BOX_IP:~/jobhunt/
```

Replace `USER@BOX_IP` (e.g. `ubuntu@203.0.113.10`).

> This intentionally transfers `.env` and `jobhunt.db` — that's exactly what we
> want. After it finishes, confirm both landed on the box:
>
> ```bash
> ssh USER@BOX_IP 'ls -la ~/jobhunt/.env ~/jobhunt/jobhunt.db'
> ```

## 3. Run the bootstrap on the box

```bash
ssh USER@BOX_IP
cd ~/jobhunt && bash deploy/setup.sh
```

The script installs git/curl/flock/timeout and a Python ≥ 3.11 (adding the
deadsnakes PPA automatically on Ubuntu if needed), creates `.venv`, installs
jobhunt, locks down `.env`, and installs the cron job.

If `jobhunt.db` is missing it will refuse to install cron and tell you to either
copy the DB over or run `.venv/bin/jobhunt scan --seed` first — that's the
flood-prevention guard.

## 4. Verify

```bash
crontab -l                    # should show the jobhunt line
# Prove it runs on the box (sends no alerts):
~/jobhunt/.venv/bin/jobhunt scan --force --no-notify
# Watch real scans land after the next :00 / :30:
tail -f ~/jobhunt/logs/cron.log
```

## 5. Turn OFF the Mac launchd agent

So the two machines don't double-scan, disable the old agent **on the Mac**:

```bash
# Find your agent's actual label first — it is whatever the plist in
# ~/Library/LaunchAgents is named (e.g. com.<you>.jobhunt.full):
ls ~/Library/LaunchAgents | grep jobhunt

launchctl bootout gui/$(id -u)/<THAT_LABEL>
launchctl list | grep jobhunt      # expect no output once unloaded
```

## 6. Heartbeat

The bootstrap also installs a second cron line (`# jobhunt-heartbeat`) that runs
once daily at **16:00 UTC (~9am Pacific)** and posts a liveness/status summary to
the **same Discord webhook** the scanner uses. The point is that *silence becomes
meaningful*: if the daily heartbeat stops arriving, the scanner is broken — not
merely "no new jobs today". The health verdict is green when `logs/cron.log` was
touched within the last 90 minutes (cron.log is appended on every scheduled fire,
including quiet-hour skips), red otherwise.

Test it any time:

```bash
# Preview the summary in the console — posts nothing:
~/jobhunt/.venv/bin/jobhunt heartbeat --no-notify
# Post one heartbeat to Discord right now:
~/jobhunt/.venv/bin/jobhunt heartbeat
```

## 7. Notes

- **Quiet hours + pacing travel with `config.yaml`.** They're timezone-aware, so
  a UTC cloud box behaves correctly with no extra tweaks.
- **Cron here is more robust than launchd.** The cron line wraps each run in
  `flock -n` (external lock → no overlapping scans) and `timeout 1800` (hard
  30-minute cap → a hung run can never block the next one).

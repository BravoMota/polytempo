# mac0 setup (PolyTempo production host)

Operator runbook for running PolyTempo on **mac0** under user **`jnlow`**, supervised by **LaunchDaemons** in `/Library/LaunchDaemons/`.

| Item | Value |
| --- | --- |
| Repo path | `/Users/jnlow/projects/PolyTempo` |
| Run-as user | `jnlow` |
| Logs | `logs/*.out.log`, `logs/*.err.log` (gitignored) |
| Reports | `reports/live/` (polytempo live), `reports/health/` (health bundles) |
| Secrets | `.env` in repo root (not committed) |

## Jobs

| Label | Script | Mode | Schedule |
| --- | --- | --- | --- |
| `com.polytempo.collector` | `scripts/run_collector.py` | long-lived | internal UTC slots |
| `com.polytempo.paper-bot` | `scripts/run_paper_bot.py` | long-lived | lead-time gates |
| `com.polytempo.live-node` | `scripts/run_live_node.py` | long-lived, **no `KeepAlive`** | lead gates + settlement sweep |
| `com.polytempo.calibration` | `scripts/run_daily_calibration.py --once` | calendar | **01:00 mac0 local** |
| `com.polytempo.db-backup` | `scripts/backup_databases.py --once` | calendar | **02:00 mac0 local** |
| `com.polytempo.performance-export` | `scripts/report_performance.py --all --csv …` | calendar | **03:30 mac0 local** |
| `com.polytempo.performance-viewer` | Streamlit `scripts/view_performance.py` | long-lived | **127.0.0.1:8501** (SSH/Tailscale) |

Calendar jobs use `StartCalendarInterval` (macOS local wall clock, including DST). Long-lived jobs use `KeepAlive` + `RunAtLoad` — except the live node, which handles money and is never auto-relaunched; see [deploy/launchd/README.md](../deploy/launchd/README.md).

---

## 1. Accounts

- **Admin** — installs LaunchDaemons (`sudo`), Tailscale, Homebrew, optional GUI apps.
- **`jnlow`** — day-to-day ops, owns repo, `.env`, venv, and SSH keys for `git pull`.

### Shared vs per-user on macOS

| Thing | Shared? | Notes |
| --- | --- | --- |
| Homebrew (`/opt/homebrew`, `/usr/local`) | Binaries on disk | New users need PATH in `~/.zprofile`; launchd uses `run-with-env.sh` |
| Apps in `/Applications` (VS Code, etc.) | Yes | Per-user settings under `~/Library/` |
| Tailscale | Machine-wide | Verify DB reachability as `jnlow` |
| Repo, `.venv`, `.env` | Per-user | Must live under `/Users/jnlow/projects/PolyTempo` |

As `jnlow`, if admin already installed Homebrew:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
source ~/.zprofile
which git python3 pg_dump
```

Install PostgreSQL client tools if `pg_dump` is missing (backup job needs it):

```bash
brew install libpq
# or: brew install postgresql@16
```

---

## 2. Clone repo

As `jnlow`:

```bash
mkdir -p ~/projects
cd ~/projects
git clone git@github.com:YOUR_ORG/PolyTempo.git PolyTempo
cd PolyTempo
```

---

## 3. Python venv

```bash
cd /Users/jnlow/projects/PolyTempo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,view]"
```

---

## 4. Environment file

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with production URLs (Tailscale host, four DB names). Minimum for production jobs:

- `POLYTEMPO_DATABASE_URL` — weather prod (`polytempo`)
- `POLYTEMPO_PAPER_DATABASE_URL` — paper prod (`polytempo_paper`)
- `POLYTEMPO_LIVE_DATABASE_URL` — live node journal (`polytempo_live`), separate from paper

Live trading credentials (`POLYMARKET_PRIVATE_KEY`, `POLYTEMPO_LIVE_CONFIRM=1`) are **not** needed while the node runs in `dry_run`; see [deploy/launchd/README.md](../deploy/launchd/README.md) before adding them.

Backup script also reads test DB URLs when set; see [database-backups.md](database-backups.md).

Load in an interactive shell:

```bash
set -a && source .env && set +a
```

---

## 5. One-time DB init

With `.env` sourced and Tailscale up:

```bash
python scripts/init_weather_db.py
python scripts/init_paper_db.py
python scripts/init_live_db.py
```

Optional first-time calibration store (before nightly job; requires this repo version with metric °C observation fetch):

```bash
python scripts/init_weather_db.py   # applies nullable observed_tmax_f migration if needed
python scripts/bootstrap_calibration_store.py
```

---

## 6. Smoke tests (before launchd)

Run each job once manually as `jnlow`:

```bash
deploy/bin/run-with-env.sh scripts/run_collector.py --once
deploy/bin/run-with-env.sh scripts/run_paper_bot.py --once
deploy/bin/run-with-env.sh scripts/run_live_node.py --once
deploy/bin/run-with-env.sh scripts/run_daily_calibration.py --once
deploy/bin/run-with-env.sh scripts/backup_databases.py --once
deploy/bin/run-with-env.sh scripts/report_performance.py --all --csv reports/performance/daily.csv
deploy/bin/run-with-env.sh -m streamlit run scripts/view_performance.py -- reports/performance/daily.csv --server.headless=true --server.address=127.0.0.1
```

Fix any connectivity or missing-tool errors before installing daemons.

**Performance viewer access** (after launchd install): from your laptop,

```bash
ssh -L 8501:127.0.0.1:8501 jnlow@mac0
```

Then open http://127.0.0.1:8501 . Nightly job refreshes `reports/performance/daily.csv`; use sidebar **Refresh from DB** for ad-hoc export.

---

## 7. macOS server hygiene

- **Time zone** — System Settings → General → Date & Time; calendar jobs follow local time.
- **Sleep** — disable sleep or jobs miss slots:

  ```bash
  sudo pmset -a sleep 0 disksleep 0 displaysleep 10
  ```

- **Tailscale** — always on; confirm `psql "$POLYTEMPO_DATABASE_URL" -c 'SELECT 1'` works as `jnlow`.
- **`pg_dump`** — must resolve when launchd runs backup (wrapper prepends Homebrew to PATH).

---

## 8. Remove old LaunchAgent (if present)

If calibration was previously loaded as a user LaunchAgent:

```bash
launchctl bootout "gui/$(id -u)/com.polytempo.calibration" 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.polytempo.calibration.plist
```

---

## 9. Install LaunchDaemons

From repo root, as admin:

```bash
sudo deploy/bin/install-launchd.sh
```

This copies plists to `/Library/LaunchDaemons/`, creates `logs/`, and `launchctl bootstrap system …` for all four labels.

Uninstall:

```bash
sudo deploy/bin/install-launchd.sh --uninstall
```

---

## 10. Verify

```bash
launchctl print system/com.polytempo.collector
launchctl print system/com.polytempo.paper-bot
launchctl print system/com.polytempo.calibration
launchctl print system/com.polytempo.db-backup

tail -f logs/collector.err.log
tail -f logs/paper-bot.err.log

# Production health bundle (launchd + logs + DB → reports/health/)
deploy/bin/polytempo-health.sh
# Attach reports/health/health_<UTC>.md to an LLM; follow the embedded review prompt.
```

One-time migration if live reports were written to `reports/` root (before `reports/live/`):

```bash
mkdir -p reports/live reports/health
mv reports/live_*.md reports/live/ 2>/dev/null || true
```

Calibration health (from any machine with DB access):

```sql
SELECT last_success_at_utc, last_target_date FROM calibration_job_state;
SELECT max(target_date) FROM calibration_observed_tmax;
```

See [deploy/launchd/README.md](../deploy/launchd/README.md) for more SQL checks.

---

## 11. Day-to-day control

Install passwordless sudo for service control (optional):

```bash
sudo visudo -f /etc/sudoers.d/polytempo
# paste contents of deploy/sudoers.d/polytempo.example
```

Then as `jnlow`:

```bash
sudo deploy/bin/polytempo-service status all
sudo deploy/bin/polytempo-service restart collector
sudo deploy/bin/polytempo-service restart paper-bot
sudo deploy/bin/polytempo-service restart live-node   # not covered by "restart all"
sudo deploy/bin/polytempo-service run calibration
sudo deploy/bin/polytempo-service run db-backup

deploy/bin/polytempo-health.sh   # LLM-ready production bundle → reports/health/
```

---

## 12. Deploy code updates

As `jnlow`, run the deploy script (pull only when `origin/main` moved, then restart):

```bash
deploy/bin/polytempo-deploy.sh
```

Optional auto-deploy on mac0 — add to `jnlow` crontab (every 10 minutes):

```cron
*/10 * * * * /Users/jnlow/projects/polytempo/deploy/bin/polytempo-deploy.sh >> /Users/jnlow/projects/polytempo/logs/deploy.log 2>&1
```

Requires passwordless sudo for `polytempo-service` (see step 11). Manual equivalent:

```bash
cd /Users/jnlow/projects/polytempo
git pull
pip install -e ".[dev,view]"   # if dependencies changed
sudo deploy/bin/polytempo-service restart all
```

Calendar jobs pick up code on their next scheduled run; use `run calibration` / `run db-backup` to test immediately.

---

## 13. Optional CI deploy

A GitHub Action (or similar) can SSH as `jnlow`, `git pull`, and `sudo deploy/bin/polytempo-service restart all`. No full machine reboot required.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Job exits immediately | `logs/*.err.log`; missing `.env` or `.venv` |
| DB timeout | Tailscale down or wrong host in `.env` |
| Backup: `pg_dump not found` | `brew install libpq`; wrapper PATH in `deploy/bin/run-with-env.sh` |
| Calibration stale | `sudo deploy/bin/polytempo-service run calibration`; SQL above |
| Duplicate calibration | Old LaunchAgent still loaded — see step 8 |

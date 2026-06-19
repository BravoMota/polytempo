# mac0 setup (PolyTempo production host)

Operator runbook for running PolyTempo on **mac0** under user **`jnlow`**, supervised by **LaunchDaemons** in `/Library/LaunchDaemons/`.

| Item | Value |
| --- | --- |
| Repo path | `/Users/jnlow/projects/PolyTempo` |
| Run-as user | `jnlow` |
| Logs | `logs/*.out.log`, `logs/*.err.log` (gitignored) |
| Secrets | `.env` in repo root (not committed) |

## Jobs

| Label | Script | Mode | Schedule |
| --- | --- | --- | --- |
| `com.polytempo.collector` | `scripts/run_collector.py` | long-lived | internal UTC slots |
| `com.polytempo.paper-bot` | `scripts/run_paper_bot.py` | long-lived | lead-time gates |
| `com.polytempo.calibration` | `scripts/run_daily_calibration.py --once` | calendar | **01:00 mac0 local** |
| `com.polytempo.db-backup` | `scripts/backup_databases.py --once` | calendar | **02:00 mac0 local** |

Calendar jobs use `StartCalendarInterval` (macOS local wall clock, including DST). Long-lived jobs use `KeepAlive` + `RunAtLoad`.

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
pip install -e ".[dev]"
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
```

Optional first-time calibration store (before nightly job):

```bash
python scripts/bootstrap_calibration_store.py
```

---

## 6. Smoke tests (before launchd)

Run each job once manually as `jnlow`:

```bash
deploy/bin/run-with-env.sh scripts/run_collector.py --once
deploy/bin/run-with-env.sh scripts/run_paper_bot.py --once
deploy/bin/run-with-env.sh scripts/run_daily_calibration.py --once
deploy/bin/run-with-env.sh scripts/backup_databases.py --once
```

Fix any connectivity or missing-tool errors before installing daemons.

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
sudo deploy/bin/polytempo-service run calibration
sudo deploy/bin/polytempo-service run db-backup
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
pip install -e ".[dev]"   # if dependencies changed
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

# Calibration job supervision (macOS / the collector Mac)

Keeps `scripts/run_daily_calibration.py` alive so the
`best_historical_updated` (bhu) calibration CSV stays current.

## Why

On 2026-06-09 the calibration store was bootstrapped and the daily job was run
once **by hand**. It was never supervised, so when the Mac restarted the job did
not come back — while the launchd-supervised collectors kept running. Result:
`calibration_observed_tmax` froze at target date 06-08 and bhu traded a stale CSV
for 8+ days. Diagnosis: `calibration_job_state.last_success_at_utc` stuck at
`2026-06-09T12:01Z` with **no** errors logged (the process never started).

## Install (LaunchAgent — runs in the logged-in user session)

```bash
# 1. Edit the three __PLACEHOLDER__ values in the plist first:
#    __REPO__ __PYTHON__ __DSN__   (see comments in the plist)
mkdir -p "$REPO/logs"
cp deploy/launchd/com.polytempo.calibration.plist ~/Library/LaunchAgents/

# 2. Smoke-test ONE real run before scheduling (backfills 06-09..yesterday from
#    the raw snapshots already collected, and confirms it works with current code):
POLYTEMPO_DATABASE_URL='postgresql://jnlow@100.76.158.32:5432/polytempo' \
  "$PYTHON" scripts/run_daily_calibration.py --once

# 3. Load the agent (starts now via RunAtLoad, then self-fires at 02:00 UTC):
launchctl load ~/Library/LaunchAgents/com.polytempo.calibration.plist

# 4. Verify it's registered and running:
launchctl list | grep com.polytempo.calibration
tail -f "$REPO/logs/calibration.err.log"
```

To stop / reload after editing:

```bash
launchctl unload ~/Library/LaunchAgents/com.polytempo.calibration.plist
launchctl load   ~/Library/LaunchAgents/com.polytempo.calibration.plist
```

### Survives logout too?

A LaunchAgent runs only while the user is logged in (same as the current
collectors). If the Mac runs headless and you need it to survive logout, install
as a **LaunchDaemon** instead: move the plist to `/Library/LaunchDaemons/`, add
`<key>UserName</key><string>your-user</string>`, `chown root:wheel`, and
`sudo launchctl load`.

## Alternative: cron (`--once` daily)

If you prefer cron over a daemon, drop the daemon and schedule `--once`. cron
uses **local time**, so 02:00 UTC = 03:00 in the Mac's WEST (UTC+1) summer:

```cron
0 3 * * *  cd /path/to/polytempo && POLYTEMPO_DATABASE_URL='postgresql://jnlow@100.76.158.32:5432/polytempo' .venv/bin/python scripts/run_daily_calibration.py --once >> logs/calibration.cron.log 2>&1
```

(The launchd daemon is preferred — it tracks the UTC anchor itself, so no DST math.)

## Verify it's actually working (from any machine with DB access)

```sql
-- last success should advance daily; observed_tmax should reach yesterday:
SELECT last_success_at_utc, last_target_date FROM calibration_job_state;
SELECT max(target_date) FROM calibration_observed_tmax;
```

# LaunchDaemons (mac0)

Production supervision for PolyTempo on mac0. **Install and operate via [docs/mac0-setup.md](../../docs/mac0-setup.md)** — clone, `.env`, smoke tests, `sudo deploy/bin/install-launchd.sh`, and `polytempo-service`.

Plists in this directory target:

- Repo: `/Users/jnlow/projects/PolyTempo`
- User: `jnlow`
- Live node: long-lived, `config/live_node.yaml` (ships as **`mode: dry_run`**) — see below
- Calibration: **01:00 local** (`--once`)
- DB backup: **02:00 local** (`--once`)
- Performance CSV export: **03:30 local** (`report_performance.py --all --csv`)
- Performance viewer: long-lived Streamlit on **127.0.0.1:8501**

## Live node (`com.polytempo.live-node`)

Runs `scripts/run_live_node.py` in a loop (lead gates, then a settlement sweep at
least every 15 min, reconcile every 6 h). Logs: `logs/live-node.{out,err}.log`.

**Environment** (from `.env`, loaded by `run-with-env.sh` — never inline secrets in a plist):

- `POLYTEMPO_LIVE_DATABASE_URL` — required. Its **own** database; the node refuses
  to start in live mode if the journal holds dry_run rows, so do not point it at
  the paper DB. Create it once with `python scripts/init_live_db.py`.
- `POLYMARKET_PRIVATE_KEY` (+ optional `POLYMARKET_WALLET_ADDRESS`) — live mode only.
- `POLYTEMPO_LIVE_CONFIRM=1` — live mode only.

**Going live takes two independent switches**: `mode: live` in
`config/live_node.yaml` **and** `POLYTEMPO_LIVE_CONFIRM=1` in `.env` (plus the
key). The plist deliberately sets neither, so editing the YAML alone can never
place a real order — the node exits instead. Reverse the change the same way:
flip the YAML back and drop the env vars.

**Kill switch** — halts all *opening* immediately; settlement still runs:

```bash
touch config/KILL_LIVE      # checked on disk every tick, no restart needed
rm config/KILL_LIVE         # resume opening
```

**No `KeepAlive`.** Unlike the other long-lived jobs this one is not relaunched
when it exits. `run_live_node.py` already swallows per-tick errors, so an actual
exit means either a clean stop or a deliberate refusal to start (failed
reconciliation, mixed-mode journal) — a relaunch loop would hammer the CLOB and
retry order paths unattended, which is worse than being down. It is likewise
excluded from `polytempo-service restart all` and therefore from
`polytempo-deploy.sh`. Read the log, then start it by name:

```bash
sudo deploy/bin/polytempo-service status live-node
sudo deploy/bin/polytempo-service restart live-node
```

`RunAtLoad` is on, so it does come back after a reboot or a daemon reinstall —
open positions still need settlement sweeps.

## Verify calibration is working

From any machine with DB access:

```sql
-- last success should advance daily; observed_tmax should reach yesterday:
SELECT last_success_at_utc, last_target_date FROM calibration_job_state;
SELECT max(target_date) FROM calibration_observed_tmax;
```

Manual trigger on mac0:

```bash
sudo deploy/bin/polytempo-service run calibration
```

## Historical note

An older **LaunchAgent** plist (UTC daemon, placeholders, DSN in plist) lived here before the mac0 LaunchDaemon bundle. Unload any copy under `~/Library/LaunchAgents/` before installing system daemons to avoid duplicate calibration runs.

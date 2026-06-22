# LaunchDaemons (mac0)

Production supervision for PolyTempo on mac0. **Install and operate via [docs/mac0-setup.md](../../docs/mac0-setup.md)** — clone, `.env`, smoke tests, `sudo deploy/bin/install-launchd.sh`, and `polytempo-service`.

Plists in this directory target:

- Repo: `/Users/jnlow/projects/PolyTempo`
- User: `jnlow`
- Calibration: **01:00 local** (`--once`)
- DB backup: **02:00 local** (`--once`)
- Performance CSV export: **03:30 local** (`report_performance.py --all --csv`)
- Performance viewer: long-lived Streamlit on **127.0.0.1:8501**

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

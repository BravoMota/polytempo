# Database backups

PolyTempo keeps four PostgreSQL databases on one host (same credentials). Backups are local `pg_dump` custom-format files under `backups/` (gitignored).

## Databases

| Logical name | Env var | Typical DB name |
| --- | --- | --- |
| weather (prod) | `POLYTEMPO_DATABASE_URL` | `polytempo` |
| weather (test) | `POLYTEMPO_TEST_DATABASE_URL` | `polytempo_test` |
| paper (prod) | `POLYTEMPO_PAPER_DATABASE_URL` | `polytempo_paper` |
| paper (test) | `POLYTEMPO_PAPER_TEST_DATABASE_URL` | `polytempo_paper_test` |

`POLYTEMPO_DATABASE_URL` falls back to `DATABASE_URL` for the weather prod DB only.

## Run backups

Source `.env` on the **Postgres host** (the Tailscale Mac), then start the daemon (default: daily **03:00 UTC**):

```bash
python scripts/backup_databases.py
```

One-shot manual run:

```bash
python scripts/backup_databases.py --once
```

Each run writes one date folder and dumps all four DBs. Retention defaults to **14 days** (older date folders are removed after a successful run).

Override output location:

```bash
export POLYTEMPO_BACKUP_DIR=/path/to/backups
python scripts/backup_databases.py --once
```

Or pass `--output-dir`. See `python scripts/backup_databases.py --help` for `--only`, `--retention-days`, `--skip-missing`, and `--dry-run` (with `--once`).

## Output layout

```
backups/
  2026-06-14/
    polytempo_20260614T030012Z.dump
    polytempo_test_20260614T030045Z.dump
    polytempo_paper_20260614T030102Z.dump
    polytempo_paper_test_20260614T030118Z.dump
```

Files use PostgreSQL **custom format** (`pg_dump -Fc`). Restore with `pg_restore`, not `psql`.

## Restore examples

Replace the dump path and target URL as needed. `--clean --if-exists` drops objects before recreating them.

Weather prod:

```bash
pg_restore --clean --if-exists --no-owner --no-acl \
  -d "$POLYTEMPO_DATABASE_URL" \
  backups/2026-06-14/polytempo_20260614T030012Z.dump
```

Weather test:

```bash
pg_restore --clean --if-exists --no-owner --no-acl \
  -d "$POLYTEMPO_TEST_DATABASE_URL" \
  backups/2026-06-14/polytempo_test_20260614T030045Z.dump
```

Paper prod:

```bash
pg_restore --clean --if-exists --no-owner --no-acl \
  -d "$POLYTEMPO_PAPER_DATABASE_URL" \
  backups/2026-06-14/polytempo_paper_20260614T030102Z.dump
```

Paper test:

```bash
pg_restore --clean --if-exists --no-owner --no-acl \
  -d "$POLYTEMPO_PAPER_TEST_DATABASE_URL" \
  backups/2026-06-14/polytempo_paper_test_20260614T030118Z.dump
```

## Scheduling

Default mode is a long-running process: sleeps until the next **03:00 UTC** slot, runs all four dumps, prunes old folders, repeats. One hour after nightly calibration (02:00 UTC).

Use `--once` for a single run (cron-friendly until unified scheduling exists). Stop the daemon with Ctrl+C or SIGTERM.

#!/usr/bin/env bash
# Collect launchd status, log tails, and DB freshness into reports/health/ for LLM review.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${POLYTEMPO_REPO:-$REPO}"
SERVICE="$REPO/deploy/bin/polytempo-service"
LINES_ERR="${POLYTEMPO_HEALTH_ERR_LINES:-80}"
LINES_OUT="${POLYTEMPO_HEALTH_OUT_LINES:-30}"
DEPLOY_LOG_LINES=30
SKIP_DB=0
OUT=""

JOBS=(collector paper-bot live-node calibration db-backup performance-export performance-viewer)

usage() {
  cat <<EOF
Usage: $0 [OPTIONS] [OUTPUT_PATH]

Write a production health bundle (raw dumps for LLM review) to:
  reports/health/health_<UTC>.md

Options:
  --no-db       Skip PostgreSQL queries (launchd status + logs only)
  -h, --help    Show this help

Optional env (see .env.example):
  POLYTEMPO_REPO, POLYTEMPO_HEALTH_ERR_LINES, POLYTEMPO_HEALTH_OUT_LINES
  POLYTEMPO_DATABASE_URL, POLYTEMPO_PAPER_DATABASE_URL (from .env)
EOF
}

for prefix in /opt/homebrew /usr/local; do
  if [[ -d "$prefix/bin" ]]; then
    PATH="$prefix/bin:$PATH"
  fi
done
export PATH

load_env() {
  local env_file="$REPO/.env"
  if [[ ! -f "$env_file" ]]; then
    echo "polytempo-health: missing $env_file" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  set -a
  source "$env_file"
  set +a
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-db)
        SKIP_DB=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        echo "Unknown option: $1" >&2
        usage
        exit 1
        ;;
      *)
        if [[ -n "$OUT" ]]; then
          echo "Unexpected extra argument: $1" >&2
          usage
          exit 1
        fi
        OUT="$1"
        shift
        ;;
    esac
  done
  if [[ $# -gt 0 ]]; then
    OUT="$1"
  fi
}

default_out_path() {
  echo "$REPO/reports/health/health_$(date -u +%Y%m%dT%H%M%SZ).md"
}

section() {
  printf '\n## %s\n\n' "$1"
}

fenced() {
  printf '```\n'
  cat
  printf '```\n'
}

write_metadata() {
  section "Metadata"
  {
    echo "generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host: $(hostname)"
    echo "repo: $REPO"
    echo "git: $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "output: $OUT"
  } | fenced
}

write_llm_prompt() {
  section "LLM review prompt"
  fenced <<'EOF'
Attach this file and respond using only the evidence below.

Give a TLDR in this exact structure:

**Overall:** GREEN | YELLOW | RED — one sentence why.

**Jobs**
- collector: OK / issue — …
- paper-bot: OK / issue — …
- live-node: OK / issue — …
- calibration: OK / issue — …
- db-backup: OK / issue — …
- performance-export: OK / issue — …
- performance-viewer: OK / issue — …

**Data freshness**
- collector_state: OK / stale — last success …
- paper bot last_tick: OK / stale — …
- calibration: OK / stale — last success …
- performance_csv: OK / stale — generated_utc …

**Action needed:** none | list concrete next steps (commands only, no speculation).

Rules:
- Flag only real problems (crashes, stale heartbeats, repeated errors, failed SQL).
- Ignore routine INFO/log noise unless it repeats or correlates with a failure.
- Long-lived jobs should be state=running with a PID.
- live-node has no KeepAlive: if it is not running it stays down. Report that and
  recommend a manual restart, never treat it as self-healing.
- paper_bot_state.last_tick should be within ~20 minutes.
- calibration_job_state.last_success_at_utc should be within ~36 hours.
- reports/performance/daily.csv generated_utc should be within ~36 hours.
- Do not quote log lines; summarize only.
- Do not invent issues not supported by this bundle.
EOF
}

write_launchd_status() {
  section "Launchd status"
  if [[ ! -x "$SERVICE" ]]; then
    echo "(polytempo-service not found at $SERVICE)" | fenced
    return
  fi
  {
    sudo "$SERVICE" status all 2>&1
  } | fenced || echo "(status failed — need sudo?)" | fenced
}

write_log_tail() {
  local job="$1"
  local stream="$2"
  local lines="$3"
  local log="$REPO/logs/${job}.${stream}.log"
  section "Log: ${job}.${stream}.log (last ${lines})"
  if [[ -f "$log" ]]; then
    tail -n "$lines" "$log" | fenced
  else
    echo "(missing)" | fenced
  fi
}

write_deploy_log() {
  section "Log: deploy.log (last ${DEPLOY_LOG_LINES})"
  local log="$REPO/logs/deploy.log"
  if [[ -f "$log" ]]; then
    tail -n "$DEPLOY_LOG_LINES" "$log" | fenced
  else
    echo "(missing)" | fenced
  fi
}

run_psql() {
  local url="$1"
  local sql="$2"
  if ! command -v psql >/dev/null 2>&1; then
    echo "(psql not found in PATH)"
    return 1
  fi
  psql "$url" -v ON_ERROR_STOP=1 -At -c "$sql" 2>&1
}

write_weather_db() {
  section "DB: weather"
  if [[ -z "${POLYTEMPO_DATABASE_URL:-}" ]]; then
    echo "(POLYTEMPO_DATABASE_URL not set)" | fenced
    return
  fi
  {
    echo "-- collector_state"
    run_psql "$POLYTEMPO_DATABASE_URL" \
      "SELECT collector_name, station_id, source, last_success_at_utc, last_error_at_utc, last_error_message, success_count, error_count FROM collector_state ORDER BY collector_name, station_id, source;" \
      || echo "(query failed)"
    echo
    echo "-- calibration_job_state"
    run_psql "$POLYTEMPO_DATABASE_URL" \
      "SELECT job_name, last_success_at_utc, last_target_date, last_error_at_utc, last_error_message FROM calibration_job_state;" \
      || echo "(query failed)"
    echo
    echo "-- latest_obs"
    run_psql "$POLYTEMPO_DATABASE_URL" \
      "SELECT max(target_date) FROM calibration_observed_tmax;" \
      || echo "(query failed)"
  } | fenced
}

write_paper_db() {
  section "DB: paper"
  if [[ -z "${POLYTEMPO_PAPER_DATABASE_URL:-}" ]]; then
    echo "(POLYTEMPO_PAPER_DATABASE_URL not set)" | fenced
    return
  fi
  {
    echo "-- paper_bot_state"
    run_psql "$POLYTEMPO_PAPER_DATABASE_URL" \
      "SELECT key, value_json, updated_at_utc FROM paper_bot_state WHERE key IN ('last_tick', 'last_monitor');" \
      || echo "(query failed)"
    echo
    echo "-- open_positions"
    run_psql "$POLYTEMPO_PAPER_DATABASE_URL" \
      "SELECT count(*) FROM paper_open_positions;" \
      || echo "(query failed)"
  } | fenced
}

write_performance_csv() {
  section "Performance CSV"
  local csv="$REPO/reports/performance/daily.csv"
  if [[ ! -f "$csv" ]]; then
    echo "(missing $csv)" | fenced
    return
  fi
  {
    head -n 1 "$csv"
    echo "mtime: $(date -r "$csv" -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || stat -f %Sm -t '%Y-%m-%dT%H:%M:%SZ' "$csv")"
    echo "size_bytes: $(wc -c <"$csv" | tr -d ' ')"
  } | fenced
}

write_reviewer_checklist() {
  section "Reviewer checklist"
  fenced <<'EOF'
- Long-lived jobs (collector, paper-bot, live-node, performance-viewer): state=running, PID set
- live-node: not running = stopped until a human restarts it (no KeepAlive)
- paper_bot_state.last_tick: within ~20 minutes
- calibration_job_state.last_success_at_utc: within ~36 hours
- collector_state.last_success_at_utc: recent per expected slot cadence
- No repeating tracebacks in *.err.log tails
EOF
}

main() {
  parse_args "$@"

  if [[ -z "$OUT" ]]; then
    OUT="$(default_out_path)"
  fi

  mkdir -p "$(dirname "$OUT")"
  load_env

  {
    echo "# PolyTempo health bundle"
    write_metadata
    write_llm_prompt
    write_launchd_status
    for job in "${JOBS[@]}"; do
      write_log_tail "$job" err "$LINES_ERR"
      write_log_tail "$job" out "$LINES_OUT"
    done
    write_deploy_log
    write_performance_csv
    if [[ "$SKIP_DB" -eq 0 ]]; then
      write_weather_db
      write_paper_db
    else
      section "DB"
      echo "(skipped — --no-db)" | fenced
    fi
    write_reviewer_checklist
  } >"$OUT"

  echo "Wrote $OUT"
}

main "$@"

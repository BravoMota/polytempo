#!/usr/bin/env bash
# Pull latest main on mac0 and restart long-lived LaunchDaemons when HEAD changed.
# Run manually as jnlow, or on a cron schedule (see header below).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${POLYTEMPO_REPO:-$REPO}"
BRANCH="${POLYTEMPO_DEPLOY_BRANCH:-main}"
REMOTE="${POLYTEMPO_DEPLOY_REMOTE:-origin}"
SERVICE="$REPO/deploy/bin/polytempo-service"
VENV_PYTHON="$REPO/.venv/bin/python"

usage() {
  cat <<EOF
Usage: $0

Fetch $REMOTE/$BRANCH; if HEAD differs, git pull --ff-only, pip install -e ".[dev]",
then sudo polytempo-service restart all. Exits quietly when already up to date.

Optional env: POLYTEMPO_REPO, POLYTEMPO_DEPLOY_BRANCH, POLYTEMPO_DEPLOY_REMOTE

Cron example (as jnlow, every 10 minutes):
  */10 * * * * $REPO/deploy/bin/polytempo-deploy.sh >> $REPO/logs/deploy.log 2>&1
EOF
}

log() {
  printf '%s deploy: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

main() {
  case "${1:-}" in
    "" ) ;;
    -h | --help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac

  if [[ ! -d "$REPO/.git" ]]; then
    log "error: not a git repo: $REPO"
    exit 1
  fi
  if [[ ! -x "$VENV_PYTHON" ]]; then
    log "error: missing venv python: $VENV_PYTHON"
    exit 1
  fi
  if [[ ! -x "$SERVICE" ]]; then
    log "error: missing $SERVICE"
    exit 1
  fi

  cd "$REPO"
  git fetch "$REMOTE" "$BRANCH"

  if git diff --quiet HEAD "$REMOTE/$BRANCH"; then
    log "already up to date ($REMOTE/$BRANCH)"
    exit 0
  fi

  log "updating $REMOTE/$BRANCH"
  git pull --ff-only "$REMOTE" "$BRANCH"
  "$VENV_PYTHON" -m pip install -e ".[dev]" -q
  sudo "$SERVICE" restart all
  log "done"
}

main "$@"

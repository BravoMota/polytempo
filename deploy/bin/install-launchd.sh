#!/usr/bin/env bash
# Install PolyTempo LaunchDaemons on mac0 (run with sudo).
set -euo pipefail

REPO="${POLYTEMPO_REPO:-/Users/jnlow/projects/PolyTempo}"
RUN_USER="${POLYTEMPO_RUN_USER:-jnlow}"
LAUNCHD_DIR="/Library/LaunchDaemons"

LABELS=(
  com.polytempo.collector
  com.polytempo.paper-bot
  com.polytempo.calibration
  com.polytempo.db-backup
)

usage() {
  cat <<EOF
Usage: sudo $0 [--uninstall]

Install PolyTempo LaunchDaemons from $REPO/deploy/launchd/ into $LAUNCHD_DIR.
Runs jobs as user $RUN_USER. Requires repo, .venv, and .env under $REPO.

  --uninstall   bootout and remove installed plists
EOF
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root (sudo)." >&2
    exit 1
  fi
}

verify_prereqs() {
  if ! id "$RUN_USER" &>/dev/null; then
    echo "User $RUN_USER does not exist." >&2
    exit 1
  fi
  for path in "$REPO" "$REPO/.venv/bin/python" "$REPO/.env" "$REPO/deploy/bin/run-with-env.sh"; do
    if [[ ! -e "$path" ]]; then
      echo "Missing prerequisite: $path" >&2
      exit 1
    fi
  done
  for label in "${LABELS[@]}"; do
    if [[ ! -f "$REPO/deploy/launchd/${label}.plist" ]]; then
      echo "Missing plist: $REPO/deploy/launchd/${label}.plist" >&2
      exit 1
    fi
  done
}

bootout_label() {
  local label="$1"
  launchctl bootout "system/${label}" 2>/dev/null || true
}

bootstrap_label() {
  local label="$1"
  local dest="${LAUNCHD_DIR}/${label}.plist"
  launchctl bootstrap system "$dest"
}

install_plists() {
  verify_prereqs
  mkdir -p "$REPO/logs"
  chown "$RUN_USER:staff" "$REPO/logs"
  chmod +x "$REPO/deploy/bin/run-with-env.sh"

  for label in "${LABELS[@]}"; do
    bootout_label "$label"
    cp "$REPO/deploy/launchd/${label}.plist" "${LAUNCHD_DIR}/${label}.plist"
    chown root:wheel "${LAUNCHD_DIR}/${label}.plist"
    chmod 644 "${LAUNCHD_DIR}/${label}.plist"
    bootstrap_label "$label"
    echo "Loaded system/${label}"
  done

  echo
  echo "Installed. Check status, e.g.:"
  echo "  launchctl print system/com.polytempo.collector"
  echo "  tail -f $REPO/logs/collector.err.log"
}

uninstall_plists() {
  for label in "${LABELS[@]}"; do
    bootout_label "$label"
    rm -f "${LAUNCHD_DIR}/${label}.plist"
    echo "Removed system/${label}"
  done
}

main() {
  local action=install
  if [[ "${1:-}" == "--uninstall" ]]; then
    action=uninstall
  elif [[ $# -gt 0 ]]; then
    usage
    exit 1
  fi

  require_root

  if [[ "$action" == "uninstall" ]]; then
    uninstall_plists
  else
    install_plists
  fi
}

main "$@"

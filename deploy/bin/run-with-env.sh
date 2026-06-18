#!/usr/bin/env bash
# Source repo .env and run a script with the project venv Python.
# Used by mac0 LaunchDaemons (launchd does not load shell profiles).
set -euo pipefail

REPO="${POLYTEMPO_REPO:-/Users/jnlow/projects/PolyTempo}"

for prefix in /opt/homebrew /usr/local; do
  if [[ -d "$prefix/bin" ]]; then
    PATH="$prefix/bin:$PATH"
  fi
done
export PATH

ENV_FILE="$REPO/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "run-with-env.sh: missing $ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

PYTHON="$REPO/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "run-with-env.sh: missing venv python at $PYTHON" >&2
  exit 1
fi

exec "$PYTHON" "$@"

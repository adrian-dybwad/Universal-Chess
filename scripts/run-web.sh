#!/usr/bin/env bash
set -euo pipefail

# Run relative to repo, not ~ or a hardcoded home
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DO_UPDATE=true
if [ "${1:-}" = "--no-update" ] || [ "${1:-}" = "--no-git-pull" ]; then
  DO_UPDATE=false
  shift
fi

cd "${REPO_ROOT}"

if [ "$DO_UPDATE" = true ]; then
  if [ ! -d "${REPO_ROOT}/.git" ]; then
    echo "Not a git repository: ${REPO_ROOT} (skipping git pull)" >&2
  elif ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Working tree has local changes; skipping 'git pull --ff-only'." >&2
    echo "Run with --no-update to silence this, or commit/stash to enable pulling." >&2
  else
    git pull --ff-only || echo "git pull failed; continuing without update." >&2
  fi
fi

if [ ! -d "${REPO_ROOT}/.venv" ]; then
  echo "No .venv found at ${REPO_ROOT}/.venv"
  echo "Create one with (from repo root):"
  echo "   cd \"${REPO_ROOT}\" && python3 -m venv --system-site-packages .venv && source .venv/bin/activate && pip install -r src/universalchess/setup/requirements.txt"
  echo "If you're currently in scripts/:"
  echo "   cd .. && python3 -m venv --system-site-packages .venv && source .venv/bin/activate && pip install -r src/universalchess/setup/requirements.txt"
  exit 1
fi

# Stop web service if it exists (best-effort)
if systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "universal-chess-web.service"; then
	sudo systemctl stop universal-chess-web.service 2>/dev/null || true
fi

# shellcheck disable=SC1091
source "${REPO_ROOT}/.venv/bin/activate"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

cd "${REPO_ROOT}/src/universalchess/web"
export FLASK_APP=app.py
python -m flask run --host=0.0.0.0 --port=5000

# Deactivate when done
deactivate

# Start web service if it exists
if systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "universal-chess-web.service"; then
	sudo systemctl start universal-chess-web.service 2>/dev/null || true
fi
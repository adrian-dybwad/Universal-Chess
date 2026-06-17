#!/usr/bin/env bash
# ============================================================================
# Universal-Chess Launcher
# ============================================================================
#
# Description:
#   Launch script for Universal-Chess project. Supports running on Bullseye,
#   Bookworm, or desktop environments. Automatically manages the Python
#   virtual environment and systemd service lifecycle.
#
# Usage:
#   ./run.sh [MODULE] [MODULE_ARGS...]
#   ./run.sh sf [SIDE] [ELO]
#   ./run.sh ENGINE [SIDE] [PRESET]
#
# Arguments:
#   MODULE        Optional Python module to run (e.g., universalchess.main)
#                 If not specified, defaults to universalchess.main
#   MODULE_ARGS   Optional arguments to pass to the Python module
#
#   Special Modes:
#   sf            (legacy) Stockfish mode is no longer wired in this launcher
#     SIDE        Side to play (white|black|random), default: white
#     ELO         Stockfish ELO (1350-2850 recommended), default: 2000
#
#   ENGINE        Runs UCI engine mode (ct800|maia|rodentiv|zahak)
#     SIDE        Side to play (white|black|random), default: random
#     PRESET      Engine preset from .uci file (e.g., "DEFAULT", "1200 ELO")
#                 If not specified, uses the DEFAULT preset from the engine's .uci file
#
# Examples:
#   ./run.sh
#     - Runs the default menu module
#
#   ./run.sh universalchess.main
#     - Explicitly runs the main app module
#
#   ./run.sh universalchess.main --debug
#     - Runs the main app module with --debug flag
#
#   ./run.sh universalchess.tests.test_paths_fen
#     - Runs a specific test module
#
#   ./run.sh sf
#     - Runs Stockfish mode with defaults (white side, ELO 2000)
#
#   ./run.sh sf black 2500
#     - Runs Stockfish mode playing as black at ELO 2500
#
#   ./run.sh sf random 1800
#     - Runs Stockfish mode with random side at ELO 1800
#
#   ./run.sh rodentiv
#     - Runs RodentIV engine with random side and DEFAULT preset
#
#   ./run.sh rodentiv white
#     - Runs RodentIV engine as white with DEFAULT preset
#
#   ./run.sh rodentiv black "1200 ELO"
#     - Runs RodentIV engine as black with "1200 ELO" preset from rodentIV.uci
#
#   ./run.sh maia random
#     - Runs Maia engine with random side and DEFAULT preset
#
#   ./run.sh zahak white
#     - Runs Zahak engine as white with DEFAULT preset
#
# Environment:
#   - Requires Python virtual environment at repo-root .venv
#   - Automatically stops/starts universal-chess.service if present
#   - Sets PYTHONPATH to include the repo src/ directory
#
# Exit Codes:
#   0   - Success
#   1   - Error (missing virtual environment, directory change failed, etc.)
#
# ============================================================================

# ./run.sh universalchess.main --device-name "MILLENNIUM CHESS" --shadow-target "Chessnut Air" --relay
# ./run.sh universalchess.main --device-name "MILLENNIUM CHESS" --shadow-target "MILLENNIUM CHESS" --relay

# Always run relative to the repo (not ~ or a hardcoded home)
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

# Ensure virtualenv exists (repo-root .venv)
if [ ! -d "${REPO_ROOT}/.venv" ]; then
  echo "No .venv found at ${REPO_ROOT}/.venv"
  echo "Create one with (from repo root):"
  echo "   cd \"${REPO_ROOT}\" && python3 -m venv --system-site-packages .venv && source .venv/bin/activate && pip install -r src/universalchess/setup/requirements.txt"
  echo "If you're currently in scripts/:"
  echo "   cd .. && python3 -m venv --system-site-packages .venv && source .venv/bin/activate && pip install -r src/universalchess/setup/requirements.txt"
  exit 1
fi

# Activate the virtual environment
# shellcheck disable=SC1091
source "${REPO_ROOT}/.venv/bin/activate"

# Ensure src-layout imports work
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

# Stop service if it exists (best-effort)
if systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "universal-chess.service"; then
	sudo systemctl stop universal-chess.service 2>/dev/null || true
fi

# Parse command line arguments
# Supports special modes (e.g., "sf" for Stockfish, engine names for UCI mode)
# and general Python module execution
DEFAULT_MODULE="universalchess.main"

# Function to check if a string is in the engines list (case-insensitive)
is_engine() {
  local search="$1"
  local engine
  for engine in "${AVAILABLE_ENGINES[@]}"; do
    if [[ "${search,,}" == "${engine}" ]]; then
      return 0
    fi
  done
  return 1
}

if [ $# -eq 0 ]; then
  # No arguments provided - use default module with no args
  MODULE="$DEFAULT_MODULE"
  MODULE_ARGS=()
elif [[ "$1" == universalchess.* ]] || [[ "$1" == *"."* ]]; then
  # First argument looks like a module path - use it as the module
  MODULE="$1"
  shift
  MODULE_ARGS=("$@")
else
  # First argument doesn't look like a module - treat all args as arguments to default module
  MODULE="$DEFAULT_MODULE"
  MODULE_ARGS=("$@")
fi

# Launch the specified Python module
echo "Launching: python -m $MODULE ${MODULE_ARGS[*]}"
python -m "$MODULE" "${MODULE_ARGS[@]}"

# Deactivate when done
deactivate

# Start web service if it exists
#if systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "universal-chess.service"; then
#	sudo systemctl start universal-chess.service 2>/dev/null || true
#fi

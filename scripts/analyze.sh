#!/usr/bin/env bash
# ============================================================================
# Universal-Chess local static-analysis runner
# ============================================================================
#
# Runs the local static-analysis toolchain over the application source.
#
# BLOCKING (these decide the exit code - the CI gate / pre-commit fails on them):
#   1. ruff --select S  - security lint subset (flake8-bandit rules)
#   2. bandit           - dedicated Python security linter (all severities)
#   3. semgrep          - taint-aware analysis: path-injection ("Uncontrolled
#                         data used in a path expression") + security rule packs
#
# REPORT-ONLY (printed for awareness, never fails the build):
#   4. ruff (ALL rules) - full lint incl. style; the legacy tree has tens of
#                         thousands of mostly-stylistic findings, so this is
#                         informational until a baseline is cleaned up.
#
# All sections run even if an earlier one reports findings.
#
# Usage:
#   ./scripts/analyze.sh                 # blocking security set + report-only ruff
#   ./scripts/analyze.sh ruff-sec        # only the blocking ruff security subset
#   ./scripts/analyze.sh bandit semgrep  # a subset of the blocking tools
#   ./scripts/analyze.sh ruff-all        # only the report-only full ruff
#
# Environment:
#   SEMGREP_OFFLINE=1   Skip the registry rule packs (network); run only the
#                       local rules in .semgrep/. Useful with no internet.
#
# Setup (once):
#   .venv/bin/python -m pip install -r requirements-dev.txt
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Prefer tools from the project venv, fall back to PATH.
VENV_BIN="${REPO_ROOT}/.venv/bin"
RUFF="${VENV_BIN}/ruff";       [[ -x "${RUFF}" ]]    || RUFF="ruff"
BANDIT="${VENV_BIN}/bandit";   [[ -x "${BANDIT}" ]]  || BANDIT="bandit"
SEMGREP="${VENV_BIN}/semgrep"; [[ -x "${SEMGREP}" ]] || SEMGREP="semgrep"

SRC="src/universalchess"

# Keep semgrep's settings/cache inside the repo (gitignored) and silence the
# network version-check + telemetry so runs are deterministic and offline-safe.
export SEMGREP_SETTINGS_FILE="${REPO_ROOT}/.cache/semgrep/settings.yml"
export SEMGREP_ENABLE_VERSION_CHECK=0
export SEMGREP_SEND_METRICS=off
mkdir -p "${REPO_ROOT}/.cache/semgrep"

# Registry packs (broad security coverage). Fetched once then cached under
# .cache. Skipped entirely when SEMGREP_OFFLINE=1.
SEMGREP_PACKS=(
    --config "p/security-audit"
    --config "p/python"
    --config "p/flask"
    --config "p/secrets"
    --config "p/command-injection"
)

TOOLS=("$@")
[[ ${#TOOLS[@]} -eq 0 ]] && TOOLS=(ruff-sec bandit semgrep ruff-all)

overall_rc=0
run_summary=""

# Record a BLOCKING tool's result (affects the overall exit code).
_record_block() {
    local name="$1" rc="$2"
    if [[ "${rc}" -eq 0 ]]; then
        run_summary+=$'\n'"  PASS    ${name}"
    else
        run_summary+=$'\n'"  FAIL    ${name} (exit ${rc})"
        overall_rc=1
    fi
}

# Record a REPORT-ONLY tool's result (never affects the exit code).
_record_report() {
    run_summary+=$'\n'"  (report) $1"
}

_run_semgrep() {
    local local_cfg=(--config "${REPO_ROOT}/.semgrep")
    local common=(--error --disable-version-check --metrics=off
        --exclude=web-app --exclude=dist --exclude=.venv --exclude=react-app)
    if [[ "${SEMGREP_OFFLINE:-0}" == "1" ]]; then
        echo "(SEMGREP_OFFLINE=1: local .semgrep rules only)"
        "${SEMGREP}" "${local_cfg[@]}" "${common[@]}" "${SRC}"
    else
        "${SEMGREP}" "${SEMGREP_PACKS[@]}" "${local_cfg[@]}" "${common[@]}" "${SRC}"
    fi
}

for tool in "${TOOLS[@]}"; do
    case "${tool}" in
        ruff-sec)
            echo "============================================================"
            echo ">>> ruff --select S (security, BLOCKING)"
            echo "============================================================"
            "${RUFF}" check --select S "${SRC}"
            _record_block "ruff-sec" "$?"
            ;;
        bandit)
            echo "============================================================"
            echo ">>> bandit (all severities, BLOCKING)"
            echo "============================================================"
            "${BANDIT}" -c pyproject.toml -r "${SRC}"
            _record_block "bandit" "$?"
            ;;
        semgrep)
            echo "============================================================"
            echo ">>> semgrep (path-injection + security packs, BLOCKING)"
            echo "============================================================"
            _run_semgrep
            _record_block "semgrep" "$?"
            ;;
        ruff-all)
            echo "============================================================"
            echo ">>> ruff (ALL rules, REPORT-ONLY)"
            echo "============================================================"
            # Report-only: print findings but never fail the build.
            "${RUFF}" check "${SRC}" --statistics || true
            _record_report "ruff-all (informational; not gated)"
            ;;
        *)
            echo "Unknown tool: ${tool}" >&2
            echo "Expected one of: ruff-sec bandit semgrep ruff-all" >&2
            exit 2
            ;;
    esac
done

echo
echo "============================================================"
echo "Static-analysis summary:${run_summary}"
echo "============================================================"
exit "${overall_rc}"

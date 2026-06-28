#!/usr/bin/env bash
# ============================================================================
# Universal-Chess local static-analysis runner
# ============================================================================
#
# Runs the local static-analysis toolchain over the application source
# (src/universalchess) and the standalone helper scripts (scripts/).
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
#   ANALYZE_FILES=...   Whitespace-separated list of files. When set, ruff-sec
#                       and bandit scan only those (CI changed-files gate);
#                       semgrep still runs whole-tree. Empty = whole tree.
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
# Python lives in two trees: the application source and the standalone helper
# scripts under scripts/ (VM-setup relays/proxies). Both are scanned so findings
# like B104 "binding to all interfaces" in scripts/ are gated, not just in src/.
SCAN_PATHS=("${SRC}" "scripts")

# Optional changed-files mode. When ANALYZE_FILES is set to a whitespace-
# separated list of paths (CI exports the files changed in the push/PR), all
# blocking linters - ruff (security subset), bandit, and semgrep - scan ONLY
# those files. This gates NEW findings without being blocked by the large
# pre-existing whole-tree backlog. Unset/empty ANALYZE_FILES preserves the
# whole-tree behaviour (used by on-demand local runs and the report-only job).
# Detect whether ANALYZE_FILES is *set at all* (even to an empty string), not
# merely non-empty: CI always exports it, and an empty value means "changed-files
# mode, but nothing Python changed" -> scan nothing (pass), NOT the whole tree.
PY_TARGETS=("${SCAN_PATHS[@]}")
if [[ -n "${ANALYZE_FILES+set}" ]]; then
    PY_TARGETS=()
    for f in ${ANALYZE_FILES}; do
        # Drop non-Python and deleted/renamed-away paths so the linters get a
        # clean target list (a missing path would abort the whole run).
        [[ "${f}" == *.py && -f "${f}" ]] && PY_TARGETS+=("${f}")
    done
fi

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
        "${SEMGREP}" "${local_cfg[@]}" "${common[@]}" "${PY_TARGETS[@]}"
    else
        "${SEMGREP}" "${SEMGREP_PACKS[@]}" "${local_cfg[@]}" "${common[@]}" "${PY_TARGETS[@]}"
    fi
}

for tool in "${TOOLS[@]}"; do
    case "${tool}" in
        ruff-sec)
            echo "============================================================"
            echo ">>> ruff --select S (security, BLOCKING)"
            echo "============================================================"
            if [[ ${#PY_TARGETS[@]} -eq 0 ]]; then
                echo "(no Python files to scan; skipped)"
                _record_block "ruff-sec" 0
            else
                "${RUFF}" check --select S "${PY_TARGETS[@]}"
                _record_block "ruff-sec" "$?"
            fi
            ;;
        bandit)
            echo "============================================================"
            echo ">>> bandit (all severities, BLOCKING)"
            echo "============================================================"
            if [[ ${#PY_TARGETS[@]} -eq 0 ]]; then
                echo "(no Python files to scan; skipped)"
                _record_block "bandit" 0
            else
                "${BANDIT}" -c pyproject.toml -r "${PY_TARGETS[@]}"
                _record_block "bandit" "$?"
            fi
            ;;
        semgrep)
            echo "============================================================"
            echo ">>> semgrep (path-injection + security packs, BLOCKING)"
            echo "============================================================"
            if [[ ${#PY_TARGETS[@]} -eq 0 ]]; then
                echo "(no Python files to scan; skipped)"
                _record_block "semgrep" 0
            else
                _run_semgrep
                _record_block "semgrep" "$?"
            fi
            ;;
        ruff-all)
            echo "============================================================"
            echo ">>> ruff (ALL rules, REPORT-ONLY)"
            echo "============================================================"
            # Report-only: print findings but never fail the build.
            "${RUFF}" check "${SCAN_PATHS[@]}" --statistics || true
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

#!/usr/bin/env bash
# ============================================================================
# Universal-Chess Pi Deploy Helper
# ============================================================================
#
# Description:
#   Sync the local universalchess source tree to a running Pi over SSH and
#   restart the systemd service. Runtime-only deploy: tests, the web app, the
#   venv, engines and caches are excluded (they are not needed by the service
#   and the Pi manages its own venv/engines).
#
#   The transfer is non-destructive (no --delete): remote-only files are left
#   untouched.
#
# Usage:
#   ./scripts/deploy-to-pi.sh [options]
#
# Options:
#   -n, --dry-run      Show what would change, transfer nothing, do not restart.
#   -c, --check        Content-only diff (rsync --checksum) preview, then exit.
#                      Use to answer "is everything already deployed?".
#       --no-restart   Sync only; do not restart the service.
#   -H, --host HOST    SSH target            (default: pi@dgt.local)
#       --path PATH    Remote source dir     (default: /opt/universalchess/)
#       --service NAME systemd unit          (default: universal-chess)
#   -h, --help         Show this help and exit.
#
# Examples:
#   ./scripts/deploy-to-pi.sh                 # sync + restart + verify
#   ./scripts/deploy-to-pi.sh --dry-run       # preview by size/time
#   ./scripts/deploy-to-pi.sh --check         # preview real content diffs
#   ./scripts/deploy-to-pi.sh --host pi@1.2.3.4
#
# Exit status:
#   0 on success; non-zero if the sync fails or the service is not active.
# ============================================================================

set -euo pipefail

HOST="pi@dgt.local"
REMOTE_PATH="/opt/universalchess/"
SERVICE="universal-chess"
DRY_RUN=0
CHECK_ONLY=0
RESTART=1
SSH_OPTS="ssh -o ConnectTimeout=10"

# Source dir resolved relative to this script, so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/../src/universalchess/"

EXCLUDES=(
	--exclude='.venv'
	--exclude='web-app'
	--exclude='engines'
	--exclude='__pycache__'
	--exclude='*.pyc'
	--exclude='.DS_Store'
	--exclude='tests'
)

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		-n|--dry-run) DRY_RUN=1; shift ;;
		-c|--check) CHECK_ONLY=1; shift ;;
		--no-restart) RESTART=0; shift ;;
		-H|--host) HOST="$2"; shift 2 ;;
		--path) REMOTE_PATH="$2"; shift 2 ;;
		--service) SERVICE="$2"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
done

if [[ ! -d "$SRC_DIR" ]]; then
	echo "Source directory not found: $SRC_DIR" >&2
	exit 1
fi

# Content-only diff preview (ignores mtime): empty output means fully in sync.
if [[ $CHECK_ONLY -eq 1 ]]; then
	echo "Content diffs (checksum) vs ${HOST}:${REMOTE_PATH} ..."
	diffs="$(rsync -azn --checksum --itemize-changes "${EXCLUDES[@]}" \
		-e "$SSH_OPTS" "$SRC_DIR" "${HOST}:${REMOTE_PATH}" 2>&1 | grep -E '^<f.*[cs]' || true)"
	if [[ -z "$diffs" ]]; then
		echo "All content in sync."
	else
		echo "$diffs"
	fi
	exit 0
fi

RSYNC_FLAGS=(-az --itemize-changes "${EXCLUDES[@]}")
[[ $DRY_RUN -eq 1 ]] && RSYNC_FLAGS+=(-n)

echo "Syncing ${SRC_DIR} -> ${HOST}:${REMOTE_PATH}$([[ $DRY_RUN -eq 1 ]] && echo '  (dry-run)')"
# Filter directory-only and unchanged-dir lines for a readable file-level summary.
rsync "${RSYNC_FLAGS[@]}" -e "$SSH_OPTS" "$SRC_DIR" "${HOST}:${REMOTE_PATH}" 2>&1 \
	| grep -vE '^\.d|/$' || true

if [[ $DRY_RUN -eq 1 ]]; then
	echo "Dry-run complete; nothing transferred, service not restarted."
	exit 0
fi

if [[ $RESTART -eq 0 ]]; then
	echo "Sync complete; --no-restart given, leaving service as-is."
	exit 0
fi

echo "Restarting ${SERVICE} on ${HOST} ..."
$SSH_OPTS "$HOST" \
	"sudo systemctl restart ${SERVICE} && sleep 3 && systemctl is-active ${SERVICE} \
	&& (journalctl -u ${SERVICE} -n 15 --no-pager | grep -iE 'error|traceback|exception' \
	&& echo 'WARNING: errors seen in recent log' || echo 'no errors in recent log')"

echo "Deploy complete."

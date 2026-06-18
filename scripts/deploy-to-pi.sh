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
#       --service NAME board systemd unit    (default: universal-chess)
#       --web-service NAME web systemd unit   (default: universal-chess-web)
#   -w, --web          Build the React app (tsc + vite) and stage it into
#                      web/react-app before syncing, then mirror that dir to the
#                      Pi with --delete. Vite emits to web-app/dist and the repo
#                      tracks no built bundle, so without this the deploy ships
#                      whatever (possibly stale) bundle was last staged.
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
# Both units run from the same tree: the board controller and the Flask web
# interface. Web/template/React changes only take effect once the web unit is
# restarted, so both are restarted on every deploy.
SERVICE="universal-chess"
WEB_SERVICE="universal-chess-web"
DRY_RUN=0
CHECK_ONLY=0
RESTART=1
BUILD_WEB=0
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

usage() { sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Build the React web app and stage it into web/react-app so the deploy ships a
# current bundle. The repo tracks no built artifact (web/react-app is gitignored
# and Vite emits to web-app/dist), so without this the deploy would push whatever
# bundle was last staged there. Mirrors scripts/build.sh's stage + sw.js stamp.
build_react() {
	local web_app_dir react_dist build_ts
	web_app_dir="${SRC_DIR}web-app"
	react_dist="${SRC_DIR}web/react-app"
	if ! command -v npm >/dev/null 2>&1; then
		echo "npm not found; cannot build the React app (required for --web)." >&2
		exit 1
	fi
	if [[ ! -d "$web_app_dir" ]]; then
		echo "React source not found: $web_app_dir" >&2
		exit 1
	fi
	echo "Building React web app (tsc + vite) ..."
	(
		cd "$web_app_dir"
		[[ -d node_modules ]] || npm install
		npm run build
	)
	echo "Staging build -> ${react_dist}"
	mkdir -p "$react_dist"
	# --delete prunes old hashed bundles locally so stale assets are not staged.
	rsync -a --delete "${web_app_dir}/dist/" "${react_dist}/"
	# Stamp the service-worker cache version so PWA clients fetch the new bundle
	# instead of a cached one. GNU sed needs no backup arg; BSD/macOS sed needs ''.
	build_ts="$(date +%Y%m%d%H%M%S)"
	if [[ -f "${react_dist}/sw.js" ]]; then
		if sed --version >/dev/null 2>&1; then
			sed -i "s/__BUILD_TIMESTAMP__/${build_ts}/g" "${react_dist}/sw.js"
		else
			sed -i '' "s/__BUILD_TIMESTAMP__/${build_ts}/g" "${react_dist}/sw.js"
		fi
		echo "Stamped sw.js CACHE_VERSION=${build_ts}"
	fi
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		-n|--dry-run) DRY_RUN=1; shift ;;
		-c|--check) CHECK_ONLY=1; shift ;;
		--no-restart) RESTART=0; shift ;;
		-H|--host) HOST="$2"; shift 2 ;;
		--path) REMOTE_PATH="$2"; shift 2 ;;
		--service) SERVICE="$2"; shift 2 ;;
		--web-service) WEB_SERVICE="$2"; shift 2 ;;
		-w|--web) BUILD_WEB=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
done

if [[ ! -d "$SRC_DIR" ]]; then
	echo "Source directory not found: $SRC_DIR" >&2
	exit 1
fi

# Build + stage the web bundle first (when requested) so it is current before the
# sync. Skipped for --check, which only previews already-staged content. The
# dedicated mirror below then ships web/react-app with --delete, so exclude it
# from the main (non-destructive) sync to avoid copying it twice.
if [[ $BUILD_WEB -eq 1 && $CHECK_ONLY -eq 0 ]]; then
	build_react
	EXCLUDES+=(--exclude='/web/react-app/')
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

# Mirror the freshly built bundle with --delete so the remote bundle dir exactly
# matches the local stage -- removing the previous build's orphaned hashed assets.
if [[ $BUILD_WEB -eq 1 ]]; then
	echo "Mirroring web/react-app (with --delete) -> ${HOST}:${REMOTE_PATH}web/react-app/"
	rsync "${RSYNC_FLAGS[@]}" --delete -e "$SSH_OPTS" \
		"${SRC_DIR}web/react-app/" "${HOST}:${REMOTE_PATH}web/react-app/" 2>&1 \
		| grep -vE '^\.d|/$' || true
fi

if [[ $DRY_RUN -eq 1 ]]; then
	echo "Dry-run complete; nothing transferred, service not restarted."
	exit 0
fi

if [[ $RESTART -eq 0 ]]; then
	echo "Sync complete; --no-restart given, leaving service as-is."
	exit 0
fi

echo "Restarting ${SERVICE} and ${WEB_SERVICE} on ${HOST} ..."
$SSH_OPTS "$HOST" \
	"sudo systemctl restart ${SERVICE} ${WEB_SERVICE} && sleep 3 \
	&& for unit in ${SERVICE} ${WEB_SERVICE}; do \
		printf '%s: ' \"\$unit\"; systemctl is-active \"\$unit\"; \
	done \
	&& (journalctl -u ${SERVICE} -u ${WEB_SERVICE} -n 20 --no-pager | grep -iE 'error|traceback|exception' \
	&& echo 'WARNING: errors seen in recent log' || echo 'no errors in recent log')"

echo "Deploy complete."

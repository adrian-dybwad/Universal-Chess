#!/usr/bin/env bash
# ============================================================================
# Universal-Chess Pi Deploy Helper
# ============================================================================
#
# Description:
#   Sync the local universalchess source tree to a running Pi over SSH, restart
#   the systemd units, and verify the board is serving before reporting success.
#   Runtime-only deploy: tests, the web app, the venv, engines and caches are
#   excluded (they are not needed by the service and the Pi manages its own
#   venv/engines).
#
#   The transfer is non-destructive (no --delete): remote-only files are left
#   untouched.
#
#   Verification is delegated to lib/remote-restart-and-verify.sh, piped to the
#   board on stdin. It waits for the web interface to actually answer and fails
#   on any automatic restart in the meantime; "Deploy complete" therefore means
#   the deployed code imported and is serving, not merely that systemd accepted
#   a restart.
#
#   Provisioning (sudoers grants, the event log, swap units) is the .deb
#   postinst's job and is not repeated here: it installs to the same
#   /opt/universalchess prefix, so its path-literal grants already cover the
#   files this script syncs.
#
# Usage:
#   ./scripts/deploy-to-pi.sh [options]
#
# Options:
#   -n, --dry-run      Show what would change, transfer nothing, do not restart.
#   -c, --check        Content-only diff (rsync --checksum) preview, then exit.
#                      Use to answer "is everything already deployed?".
#       --no-restart   Sync only; do not restart the service.
#       --no-elevate   Do not run the remote rsync under sudo. Only for targets
#                      whose tree is owned by the SSH user; the .deb install
#                      tree is root-owned and needs the default elevation.
#                      A board without passwordless sudo is still elevated:
#                      the tree is staged into the SSH user's home, then
#                      `ssh -t sudo rsync` installs it so the terminal can
#                      prompt. `--rsync-path=sudo rsync` cannot prompt --
#                      rsync's remote command has no TTY.
#   -H, --host HOST    SSH target            (default: pi@dgt.local)
#       --path PATH    Remote source dir     (default: /opt/universalchess/)
#       --service NAME board systemd unit    (default: universal-chess)
#       --web-service NAME web systemd unit   (default: universal-chess-web)
#       --web-port PORT Loopback port the web unit binds  (default: 5000)
#       --verify-timeout SECS How long to wait for the web interface to serve
#                      after the restart (default: 240). The app takes ~70s to
#                      import on the board's ARMv6 core; verification returns as
#                      soon as it answers.
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
#   0  synced, restarted, and the board verified as serving
#   rsync's own status if the transfer fails (e.g. 23, partial transfer)
#   2  a unit is not running after the restart
#   3  a unit auto-restarted after the deploy, i.e. it is crashing on this code
#   4  the web interface did not serve within --verify-timeout
#
#   Codes 2-4 come from lib/remote-restart-and-verify.sh and are propagated
#   unchanged, so a caller can tell "never came up" from "came up and died".
# ============================================================================

set -euo pipefail

HOST="pi@dgt.local"
REMOTE_PATH="/opt/universalchess/"
# Both units run from the same tree: the board controller and the Flask web
# interface. Web/template/React changes only take effect once the web unit is
# restarted, so both are restarted on every deploy.
SERVICE="universal-chess"
WEB_SERVICE="universal-chess-web"

# Loopback port WEB_SERVICE binds (its ExecStart passes --port=5000) and nginx
# proxies to. Health is probed there, not through nginx, which answers 502 for
# the whole of the app's startup.
WEB_PORT=5000

# How long to wait for the web interface to serve after the restart. Importing
# the Flask app takes roughly 70 seconds on the board's single ARMv6 core, so a
# short window reports failure on a perfectly healthy board; verification exits
# as soon as the app answers, so a generous default costs a fast board nothing.
VERIFY_TIMEOUT_SECS=240
DRY_RUN=0
CHECK_ONLY=0
RESTART=1
BUILD_WEB=0
ELEVATE=1
SSH_OPTS="ssh -o ConnectTimeout=10"

# postinst runs `chown -R root:root ${DGTCM_PATH}` deliberately: the passwordless
# sudo grants are path literals under /opt/universalchess/scripts/, so a service
# user able to rewrite those files could escalate to root. Writing into that tree
# therefore requires root on the receiving side. Without this the transfer is
# denied for every file -- the failure this script previously reported as
# "Deploy complete".
REMOTE_RSYNC="sudo rsync"

# Home-relative staging dir used when remote sudo cannot run without a TTY
# (--rsync-path=sudo rsync never prompts). Filled unelevated, then installed
# with ssh -t sudo rsync so the operator's terminal can ask for a password.
REMOTE_STAGING="uc-deploy-staging/"

# -a without -o -g. A root-side receiver honours -o/-g by applying the *sender's*
# numeric uid/gid, which on macOS is 501:staff -- silently destroying the
# root:root ownership the sudoers path grants depend on. -rlptD keeps recursion,
# symlinks, modes (the exec bit on scripts/) and mtimes (so unchanged files are
# not re-sent) while leaving ownership to the receiving process.
RSYNC_ARCHIVE=(-rlptD)

# Directories the running product writes to that the sync also ships files into.
# An elevated rsync creates NEW files as root, so without a regrant a deploy that
# adds a file here leaves it unwritable by the service -- surfacing later at
# runtime, far from the deploy that caused it.
#
# This is the subset of postinst's RUNTIME_WRITABLE_DIRS that exists in the
# source tree (config, engines, tmp and pending-updates are runtime-only, so the
# sync never touches them). postinst remains the source of truth; deliberately
# NOT widened to the install root, which must stay root-owned.
RUNTIME_WRITABLE_DIRS=(db web/static)

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

# Print the header block as help, ending at the closing banner rule rather than
# at a fixed line number: the previous fixed range silently truncated --help the
# moment the header changed length.
usage() { awk 'NR>4 && /^# ={10,}/ {exit} NR>4 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"; }

# Run rsync, filter its stdout for readability, and abort the deploy on failure.
#
# The filter is applied to stdout only: merging stderr into it (the previous
# `2>&1 | grep -vE ...`) fed rsync's diagnostics through a pattern that discards
# them, so permission failures left no trace. PIPESTATUS[0] is read rather than
# relying on pipefail because grep legitimately exits 1 when it filters every
# line, which must not be mistaken for a transfer failure. `set +e` around the
# pipeline keeps errexit from aborting before that status can be inspected.
#
# Exiting with rsync's own status (not a generic 1) preserves the distinction
# between e.g. a partial transfer (23) and a protocol error for any caller.
run_rsync() {
	local rc=0
	set +e
	rsync "$@" | grep -vE '^\.d|/$'
	rc=${PIPESTATUS[0]}
	set -e
	if [[ $rc -ne 0 ]]; then
		echo "rsync failed (exit ${rc}); aborting without restarting the service." >&2
		exit "$rc"
	fi
}

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
		--no-elevate) ELEVATE=0; shift ;;
		-H|--host) HOST="$2"; shift 2 ;;
		--path) REMOTE_PATH="$2"; shift 2 ;;
		--service) SERVICE="$2"; shift 2 ;;
		--web-service) WEB_SERVICE="$2"; shift 2 ;;
		--web-port) WEB_PORT="$2"; shift 2 ;;
		--verify-timeout) VERIFY_TIMEOUT_SECS="$2"; shift 2 ;;
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
# Deliberately unelevated -- it only reads a world-readable tree, and requesting
# sudo would make a pure diagnostic fail on boards without a NOPASSWD grant. The
# same attribute set as the real transfer is compared, so the preview cannot
# report ownership diffs the deploy would never apply.
if [[ $CHECK_ONLY -eq 1 ]]; then
	echo "Content diffs (checksum) vs ${HOST}:${REMOTE_PATH} ..."
	# The probe's own status is checked before its output is interpreted: an
	# unreachable board also produces no diff lines, and reporting that as "All
	# content in sync" is the same false reassurance this script is being fixed
	# to stop giving.
	set +e
	diffs="$(rsync -n --checksum --itemize-changes "${RSYNC_ARCHIVE[@]}" \
		"${EXCLUDES[@]}" -e "$SSH_OPTS" "$SRC_DIR" "${HOST}:${REMOTE_PATH}")"
	probe_rc=$?
	set -e
	if [[ $probe_rc -ne 0 ]]; then
		echo "rsync probe failed (exit ${probe_rc}); sync state unknown." >&2
		exit "$probe_rc"
	fi
	diffs="$(printf '%s\n' "$diffs" | grep -E '^<f.*[cs]' || true)"
	if [[ -z "$diffs" ]]; then
		echo "All content in sync."
	else
		echo "$diffs"
	fi
	exit 0
fi

# NOTE: No -z (compression). macOS ships openrsync (advertises "rsync 2.6.9
# compatible"); the Pi runs rsync 3.4.1. Their zlib/compression handshake is
# incompatible and silently writes 0-byte files for newly-created paths. Plain
# -a transfers correctly over the (fast, local) network without that risk.
RSYNC_FLAGS=("${RSYNC_ARCHIVE[@]}" --itemize-changes "${EXCLUDES[@]}")
if [[ $DRY_RUN -eq 1 ]]; then
	RSYNC_FLAGS+=(-n)
fi

# sudo via --rsync-path has no TTY, so a board whose SSH user cannot
# NOPASSWD never prompts -- the operator sees "a terminal is required"
# even in a real terminal. Probe first. ssh exit 255 is unreachable, not
# "needs a password"; any other non-zero is treated as a passworded sudo.
ELEVATE_VIA_TTY=0
if [[ $ELEVATE -eq 1 && $DRY_RUN -eq 0 ]]; then
	set +e
	$SSH_OPTS -o BatchMode=yes "$HOST" "sudo -n true" >/dev/null 2>&1
	probe_rc=$?
	set -e
	if [[ $probe_rc -eq 0 ]]; then
		RSYNC_FLAGS+=(--rsync-path="$REMOTE_RSYNC")
	elif [[ $probe_rc -eq 255 ]]; then
		echo "Cannot reach ${HOST} (ssh exit 255); aborting." >&2
		exit 255
	else
		ELEVATE_VIA_TTY=1
		echo "Remote sudo requires a password; staging to ~/${REMOTE_STAGING%/} then installing as root."
	fi
elif [[ $ELEVATE -eq 1 ]]; then
	RSYNC_FLAGS+=(--rsync-path="$REMOTE_RSYNC")
fi

if [[ $ELEVATE_VIA_TTY -eq 1 ]]; then
	echo "Syncing ${SRC_DIR} -> ${HOST}:${REMOTE_STAGING}"
	run_rsync "${RSYNC_FLAGS[@]}" -e "$SSH_OPTS" "$SRC_DIR" "${HOST}:${REMOTE_STAGING}"
	if [[ $BUILD_WEB -eq 1 ]]; then
		echo "Mirroring web/react-app (with --delete) -> ${HOST}:${REMOTE_STAGING}web/react-app/"
		run_rsync "${RSYNC_FLAGS[@]}" --delete -e "$SSH_OPTS" \
			"${SRC_DIR}web/react-app/" "${HOST}:${REMOTE_STAGING}web/react-app/"
	fi
	SERVICE_USER="${HOST%%@*}"
	# One ssh -t so sudo can prompt once. Do not pipe a script: -t uses stdin
	# for the password. -rlptD, not -a: same ownership rule as the direct path.
	apply="sudo rsync -rlptD \"\$HOME/${REMOTE_STAGING}\" \"${REMOTE_PATH}\""
	if [[ $BUILD_WEB -eq 1 ]]; then
		apply+=" && sudo rsync -rlptD --delete \"\$HOME/${REMOTE_STAGING}web/react-app/\" \"${REMOTE_PATH}web/react-app/\""
	fi
	for dir in "${RUNTIME_WRITABLE_DIRS[@]}"; do
		apply+=" && sudo chown -R ${SERVICE_USER}:${SERVICE_USER} '${REMOTE_PATH%/}/${dir}'"
	done
	echo "Installing staged tree into ${REMOTE_PATH} (sudo may ask for a password) ..."
	ssh -t -o ConnectTimeout=10 "$HOST" "$apply"
else
	echo "Syncing ${SRC_DIR} -> ${HOST}:${REMOTE_PATH}$([[ $DRY_RUN -eq 1 ]] && echo '  (dry-run)')"
	run_rsync "${RSYNC_FLAGS[@]}" -e "$SSH_OPTS" "$SRC_DIR" "${HOST}:${REMOTE_PATH}"
	if [[ $BUILD_WEB -eq 1 ]]; then
		echo "Mirroring web/react-app (with --delete) -> ${HOST}:${REMOTE_PATH}web/react-app/"
		run_rsync "${RSYNC_FLAGS[@]}" --delete -e "$SSH_OPTS" \
			"${SRC_DIR}web/react-app/" "${HOST}:${REMOTE_PATH}web/react-app/"
	fi
fi

if [[ $DRY_RUN -eq 1 ]]; then
	echo "Dry-run complete; nothing transferred, service not restarted."
	exit 0
fi

# Hand the runtime data directories back to the service user, mirroring
# postinst's grantRuntimeDataOwnership. Only needed after a *direct* elevated
# sync (new files created as root). The TTY path already chowns in the same
# sudo that copies, so it does not need a second password prompt here.
if [[ $ELEVATE -eq 1 && $ELEVATE_VIA_TTY -eq 0 ]]; then
	SERVICE_USER="${HOST%%@*}"
	echo "Restoring ${SERVICE_USER} ownership of runtime data dirs ..."
	regrant=""
	for dir in "${RUNTIME_WRITABLE_DIRS[@]}"; do
		regrant+="sudo chown -R ${SERVICE_USER}:${SERVICE_USER} '${REMOTE_PATH%/}/${dir}'; "
	done
	$SSH_OPTS "$HOST" "$regrant true"
fi

if [[ $RESTART -eq 0 ]]; then
	echo "Sync complete; --no-restart given, leaving service as-is."
	exit 0
fi

# Restart and verify on the board. The verification script is piped to a remote
# bash on stdin so it always matches this checkout and needs no install step; see
# its header for why readiness is polled instead of slept on. errexit propagates
# its exit status, so "Deploy complete" is only ever printed for a board that is
# actually serving.
HEALTH_SCRIPT="${SCRIPT_DIR}/lib/remote-restart-and-verify.sh"
if [[ ! -f $HEALTH_SCRIPT ]]; then
	echo "Verification script not found: ${HEALTH_SCRIPT}" >&2
	echo "Refusing to restart without it: an unverified restart is how a" \
		"crash-looping board previously passed as a successful deploy." >&2
	exit 1
fi

echo "Restarting and verifying ${SERVICE} and ${WEB_SERVICE} on ${HOST} ..."
$SSH_OPTS "$HOST" bash -s -- \
	"$SERVICE" "$WEB_SERVICE" "$WEB_PORT" "$VERIFY_TIMEOUT_SECS" < "$HEALTH_SCRIPT"

echo "Deploy complete."

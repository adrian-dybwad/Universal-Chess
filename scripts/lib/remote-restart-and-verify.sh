#!/usr/bin/env bash
# ============================================================================
# Universal-Chess post-deploy restart + health verification (runs on the board)
# ============================================================================
#
# Description:
#   Restart the two Universal-Chess units and verify the board is actually
#   healthy afterwards: both units running, neither auto-restarted since the
#   deploy, and the web interface serving requests.
#
#   Delivered to the board over ssh on stdin (`ssh host bash -s -- <args>`) by
#   scripts/deploy-to-pi.sh, so it needs no installation step and always matches
#   the checkout being deployed. Kept as its own file rather than an inline
#   remote command so it can be executed directly against fakes; see
#   src/universalchess/tests/test_remote_health_verification.py.
#
#   Why the wait is a readiness probe and not a sleep:
#     Importing the Flask app takes roughly 70 seconds on the board's single
#     ARMv6 core. The previous `sleep 3 && systemctl is-active` reported success
#     while the app was still importing, and the app then exited 1 at +78s -- so
#     a deploy of code that could not even import reported "Deploy complete".
#     Both units set Restart=always, so `is-active` also says `active` moments
#     after every crash: a crash loop is indistinguishable from health unless
#     the automatic-restart counter is compared against a post-restart baseline.
#
# Usage:
#   remote-restart-and-verify.sh BOARD_UNIT WEB_UNIT WEB_PORT TIMEOUT_SECS
#
#   BOARD_UNIT     systemd unit for the board controller.
#   WEB_UNIT       systemd unit for the Flask web interface.
#   WEB_PORT       Loopback port WEB_UNIT binds (nginx's proxy_pass target).
#   TIMEOUT_SECS   How long to wait for the web interface to serve. 0 performs a
#                  single probe.
#
# Exit status:
#   0  both units running and the web interface serving
#   1  usage error (nothing was restarted)
#   2  a unit is not running
#   3  a unit auto-restarted after the deploy, i.e. it is crash-looping
#   4  the web interface did not serve within TIMEOUT_SECS
#
#   The failure codes are distinct because the causes and the next steps differ:
#   3 means the deployed code starts and dies (read the traceback), 4 means it
#   never finishes starting (hung import, or a port already bound).
# ============================================================================

# Deliberately no -e: the probes below fail as part of normal operation (the web
# port is closed for the first ~70s) and each failure is inspected rather than
# fatal. Failures that must abort do so via an explicit exit.
set -uo pipefail

readonly BOARD_UNIT="${1:-}"
readonly WEB_UNIT="${2:-}"
readonly WEB_PORT="${3:-}"
readonly TIMEOUT_SECS="${4:-}"

# Seconds between readiness probes. Short enough that a fast board is not held
# up, long enough that polling costs the ARMv6 core nothing measurable.
readonly POLL_INTERVAL_SECS=3

# Cheap, unauthenticated JSON endpoint the app's own clients poll; a 2xx from it
# proves the module finished importing and the request path works. Deliberately
# probed on loopback: nginx answers 502 while the app is importing, so probing
# through the proxy would report the proxy's health, not the app's.
readonly HEALTH_PATH="/api/system/activity"

# Per-probe HTTP timeout. Bounded so a hung socket cannot stall the poll loop
# past the caller's deadline.
readonly PROBE_TIMEOUT_SECS=5

readonly EXIT_USAGE=1
readonly EXIT_UNIT_NOT_ACTIVE=2
readonly EXIT_UNIT_RESTARTED=3
readonly EXIT_NEVER_READY=4

usage() {
	echo "usage: $(basename "$0") BOARD_UNIT WEB_UNIT WEB_PORT TIMEOUT_SECS" >&2
}

if [[ -z $BOARD_UNIT || -z $WEB_UNIT || -z $WEB_PORT || -z $TIMEOUT_SECS ]]; then
	usage
	exit "$EXIT_USAGE"
fi
if ! [[ $WEB_PORT =~ ^[0-9]+$ && $TIMEOUT_SECS =~ ^[0-9]+$ ]]; then
	echo "WEB_PORT and TIMEOUT_SECS must be numeric (got '${WEB_PORT}', '${TIMEOUT_SECS}')" >&2
	exit "$EXIT_USAGE"
fi

readonly UNITS=("$BOARD_UNIT" "$WEB_UNIT")

# Number of times systemd has restarted the unit on its own. Compared against a
# baseline captured right after the deploy's restart rather than against zero, so
# only crashes caused by the code just shipped are counted. An explicit
# `systemctl restart` was observed to reset the counter to 0 on the board's
# systemd, but the comparison deliberately does not rely on that: a baseline is
# correct whether or not the reset happens.
auto_restart_count() {
	systemctl show -p NRestarts --value "$1" 2>/dev/null
}

# Emit whatever explains a failure. journalctl covers systemd's view (exit
# codes, restart scheduling); the unit's log file holds the application
# traceback, because the units route stdout/stderr to append:/var/log/<unit>.log
# rather than the journal -- the reason the old journal-only grep found nothing.
# Read without sudo (the files are world-readable) so a diagnostic never fails
# for want of a sudoers grant, and skipped when absent.
report_diagnostics() {
	local unit="$1" log="/var/log/$1.log"
	echo "--- journalctl -u ${unit} (last 40) ---"
	journalctl -u "$unit" -n 40 --no-pager 2>&1
	if [[ -r $log ]]; then
		echo "--- ${log} (last 40) ---"
		tail -n 40 "$log" 2>&1
	fi
}

# Fail unless every unit is running and none has auto-restarted since baseline.
# An inactive unit and a crash-looping one are reported differently: the first
# has given up, the second is repeatedly dying on the new code.
check_units_stable() {
	local unit state current
	for unit in "${UNITS[@]}"; do
		state="$(systemctl is-active "$unit" 2>/dev/null)"
		case "$state" in
			active|activating|reloading) ;;
			*)
				echo "FAILED: ${unit} is '${state:-unknown}', not running." >&2
				report_diagnostics "$unit" >&2
				exit "$EXIT_UNIT_NOT_ACTIVE"
				;;
		esac
		current="$(auto_restart_count "$unit")"
		if [[ ${current:-0} -gt ${BASELINE_RESTARTS[$unit]:-0} ]]; then
			echo "FAILED: ${unit} restarted since the deploy (NRestarts" \
				"${BASELINE_RESTARTS[$unit]:-0} -> ${current}); it is crashing on" \
				"the deployed code." >&2
			report_diagnostics "$unit" >&2
			exit "$EXIT_UNIT_RESTARTED"
		fi
	done
}

# A closed port is the expected state for the first ~70s, so the probe is fully
# silent (-s without -S): printing curl's "Could not connect" for every poll
# filled a successful deploy's output with 20 lines that read like failures.
# Diagnostics are emitted once, by the failure paths, where they mean something.
web_is_serving() {
	curl -fs -m "$PROBE_TIMEOUT_SECS" -o /dev/null \
		"http://127.0.0.1:${WEB_PORT}${HEALTH_PATH}"
}

echo "Restarting ${BOARD_UNIT} and ${WEB_UNIT} ..."
if ! sudo systemctl restart "${UNITS[@]}"; then
	echo "FAILED: could not restart ${BOARD_UNIT} ${WEB_UNIT}." >&2
	exit "$EXIT_UNIT_NOT_ACTIVE"
fi

# Baseline captured immediately after the restart so the comparison only sees
# restarts triggered by the code just deployed.
declare -A BASELINE_RESTARTS=()
for unit in "${UNITS[@]}"; do
	BASELINE_RESTARTS["$unit"]="$(auto_restart_count "$unit")"
done

echo "Waiting up to ${TIMEOUT_SECS}s for ${WEB_UNIT} to serve" \
	"127.0.0.1:${WEB_PORT}${HEALTH_PATH} (import takes ~70s on ARMv6) ..."

# Probe before testing the deadline, so TIMEOUT_SECS=0 still performs one probe.
deadline=$((SECONDS + TIMEOUT_SECS))
while true; do
	check_units_stable
	if web_is_serving; then
		echo "OK: ${WEB_UNIT} is serving; ${BOARD_UNIT} and ${WEB_UNIT} running" \
			"with no restarts since the deploy."
		exit 0
	fi
	if ((SECONDS >= deadline)); then
		echo "FAILED: ${WEB_UNIT} did not serve 127.0.0.1:${WEB_PORT}${HEALTH_PATH}" \
			"within ${TIMEOUT_SECS}s." >&2
		report_diagnostics "$WEB_UNIT" >&2
		exit "$EXIT_NEVER_READY"
	fi
	sleep "$POLL_INTERVAL_SECS"
done

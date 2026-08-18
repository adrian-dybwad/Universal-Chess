"""Record how the previous session ended, before any hardware is touched.

The DGT controller is the board's power manager: sleeping it cuts power to the
Pi, and it can do so before the filesystem has finished unmounting. The evidence
of that is written to the OS logs at the next boot and nowhere else, so this
audit reads them at the very start of startup -- before the controller is
initialised, because initialising it is what eventually removes the power again.

The verdict is module state rather than a return value because the consumer is
far away in time: the About screen shows the warning whenever the user opens it,
long after the audit ran. It lives here rather than on the entry point so that
reading it costs an import of this module, not an import of ``main`` (which
boots the board as a side effect of being imported).

Everything else the audit collects is written to the log for a human reading a
support bundle. Only filesystem errors set the verdict, and only real errors:
"orphan cleanup on readonly fs" appears on every healthy boot, cleaning up files
that were open when the previous session ended, so treating it as damage would
show the warning permanently and train the user to ignore it.
"""

import subprocess  # nosec B404 - used only for fixed, trusted system tools below

from universalchess.board.logging import log

_shutdown_was_incomplete = False


def shutdown_was_incomplete() -> bool:
    """Return whether the previous session ended with filesystem damage.

    False until :func:`audit_previous_shutdown` has run and found evidence, so a
    process that never audits (a widget preview, the web service) reports a
    clean board rather than accusing one on no evidence.
    """
    return _shutdown_was_incomplete


def reset() -> None:
    """Clear the verdict. For tests; the audit is a once-per-boot reading."""
    global _shutdown_was_incomplete
    _shutdown_was_incomplete = False


def audit_previous_shutdown() -> bool:
    """Log every OS-level indicator of how the previous session ended.

    Captures whether the previous shutdown was clean or power was removed
    unexpectedly (typically the controller's sleep command cutting power before
    the Pi finished shutting down). Checks, in order: filesystem recovery
    messages in dmesg, the recent boot list, shutdown/reboot history from wtmp,
    and the previous boot's final journal messages.

    Every probe is best-effort and swallows its own failure: this runs before
    anything else at boot, so a missing tool must not abort startup, and an
    unreadable log is not evidence of damage.

    Returns:
        Whether filesystem errors were found, which is also recorded for
        :func:`shutdown_was_incomplete`.
    """
    global _shutdown_was_incomplete

    log.info("=" * 70)
    log.info("[Startup] PREVIOUS SHUTDOWN ANALYSIS - Checking OS indicators")
    log.info("=" * 70)

    # 1. Check dmesg for filesystem ERROR messages (not routine cleanup)
    try:
        # dmesg is a fixed, trusted system tool and the service runs with a
        # controlled PATH; the partial-path / no-shell findings are accepted.
        result = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0:
            error_indicators = []
            for line in result.stdout.split('\n'):
                line_lower = line.lower()
                # Actual errors that indicate problems
                if 'ext4-fs error' in line_lower or 'ext4_error' in line_lower:
                    error_indicators.append(line.strip())
                elif 'unclean' in line_lower:
                    error_indicators.append(line.strip())
                elif 'recovering journal' in line_lower:
                    # Journal recovery with actual data loss indication
                    error_indicators.append(line.strip())
            if error_indicators:
                _shutdown_was_incomplete = True
                log.warning("[Startup] DMESG: Filesystem errors detected (possible unclean shutdown):")
                for indicator in error_indicators[:10]:
                    log.warning(f"[Startup] DMESG:   {indicator}")
            else:
                log.info("[Startup] DMESG: No filesystem errors found (clean)")
    except Exception as e:
        log.error(f"[Startup] DMESG: Could not check dmesg: {e}")

    # 2. Check journalctl for boot list
    try:
        result = subprocess.run(["journalctl", "--list-boots", "-n", "5"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0:
            log.info("[Startup] JOURNALCTL: Recent boots:")
            for line in result.stdout.strip().split('\n')[:5]:
                if line.strip():
                    log.info(f"[Startup] JOURNALCTL:   {line.strip()}")
    except Exception as e:
        log.debug(f"[Startup] JOURNALCTL: Could not list boots: {e}")

    # 3. Check last -x for shutdown/reboot/crash entries
    try:
        result = subprocess.run(["last", "-x", "-n", "10"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0:
            log.info("[Startup] LAST -x: Recent shutdown/reboot entries:")
            for line in result.stdout.strip().split('\n')[:10]:
                if line.strip() and ('shutdown' in line.lower() or 'reboot' in line.lower() or 'crash' in line.lower()):
                    log.info(f"[Startup] LAST:   {line.strip()}")
    except Exception as e:
        log.debug(f"[Startup] LAST: Could not check last -x: {e}")

    # 4. Check previous boot's final messages
    try:
        result = subprocess.run(["journalctl", "-b", "-1", "-n", "20", "--no-pager"], capture_output=True, text=True, timeout=10)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0 and result.stdout.strip():
            log.info("[Startup] JOURNALCTL: Last 20 messages from PREVIOUS boot:")
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    log.info(f"[Startup] PREV_BOOT:   {line.strip()}")

            # Check if it reached Power-Off target (clean shutdown)
            if 'Reached target Power-Off' in result.stdout or 'Reached target Reboot' in result.stdout:
                log.info("[Startup] PREV_BOOT: Previous boot reached Power-Off/Reboot target (CLEAN shutdown)")
            elif 'Stopping' in result.stdout and 'systemd' in result.stdout.lower():
                log.info("[Startup] PREV_BOOT: Previous boot was in shutdown sequence")
            else:
                log.warning("[Startup] PREV_BOOT: No Power-Off target reached - possible abrupt power loss")
        else:
            log.info("[Startup] JOURNALCTL: No previous boot journal available (first boot or journal rotated)")
    except Exception as e:
        log.debug(f"[Startup] JOURNALCTL: Could not check previous boot: {e}")

    log.info("=" * 70)
    log.info("[Startup] PREVIOUS SHUTDOWN ANALYSIS COMPLETE")
    log.info("=" * 70)

    return _shutdown_was_incomplete

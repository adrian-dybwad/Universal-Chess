"""Pure decision logic for choosing the e-paper display driver at startup.

The board ships with the UC8151D driver (DGT Centaur V2 panel). A V1 panel uses
an SSD1680/IL3820-family controller whose BUSY line is the inverse polarity, so
the UC8151D init never sees idle and times out. When -- and only when -- that
BUSY timeout occurs, the board automatically retries with the SSD1680 driver.

The SSD1680 fallback requires *no* opt-in: a BUSY timeout is the V1-panel
signature, and the SSD1680 driver is harmless on a panel that already failed the
UC8151D path. The separate ``[display] waveform_profile`` / ``high_contrast``
settings do NOT gate this selection at all; they only choose how the SSD1680
driver then drives the panel (see ``epd2in9_ssd1680.EPD(profile=...)`` and the
``waveform_profiles`` registry). That keeps driver configuration out of this
pure branching rule.

This module holds *only* the branching rule, kept pure so it is exhaustively
unit-testable without any hardware. ``main`` performs the actual init attempts
(constructing Managers and calling ``init()``), feeds the results here as
:class:`DisplayAttempt` values, and writes the resolved :class:`DisplayOutcome`
to the cross-process display-status file.

Why alt is gated on the BUSY *timeout* specifically (not any failure): a
non-timeout failure (e.g. SPI/module init error) is a different fault that the
alternate driver would not fix, and the web UI only reveals the IL3820 opt-in
when a timeout actually occurred. Trying the alt driver on every failure would
both mislead that gating and mask unrelated faults.

Probe order is hinted by the previous boot. A V1 panel can never satisfy the
UC8151D probe, so probing it first costs the full BUSY timeout (measured at
5.1 s) on every boot to re-derive a known fact. ``hint_from_status`` reads the
controller that last drove the panel out of the status file this module's
outcome already populates, and ``controller_order`` probes it first.

The hint is persisted *observation*, not detection, so it is deliberately
self-correcting: an unusable value degrades to the shipped order, and any
failure of a hinted driver falls through to the other controller. That last
rule is why ``should_attempt_alt`` takes ``hinted`` -- the strict BUSY-timeout
gate is right when the shipped order failed (an unrelated fault must not be
masked), but wrong when the driver was chosen from unverified disk state, where
the likeliest explanation for a failure is that the hint no longer matches the
hardware. Without it a panel swap would leave the board permanently blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Controller identities reported to the System card. Kept as plain strings so
# the web layer needs no import from the hardware/driver modules.
CONTROLLER_UC8151D = "UC8151D"
CONTROLLER_SSD1680 = "SSD1680"


@dataclass(frozen=True)
class DisplayAttempt:
    """Result of one driver's startup ``init()`` attempt.

    ``busy_timeout`` is meaningful only for the primary (UC8151D) attempt: it is
    True when ``init()`` failed specifically because the BUSY line never reached
    idle within the timeout (the V1-panel signature), as opposed to any other
    initialization error.
    """

    ok: bool
    busy_timeout: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class DisplayOutcome:
    """Resolved startup state for the display subsystem.

    ``busy_timeout`` gates the web UI's IL3820 opt-in (shown only when True).
    ``active_controller`` is the controller that successfully drove the panel,
    or None when the display is disabled.
    """

    initialized: bool
    busy_timeout: bool
    active_controller: Optional[str]
    error: Optional[str]


# Probe order used when no usable hint exists: the shipped V2 driver first.
DEFAULT_CONTROLLER_ORDER = (CONTROLLER_UC8151D, CONTROLLER_SSD1680)


def hint_from_status(status: Optional[dict]) -> Optional[str]:
    """Extract the controller to probe first from a persisted display status.

    ``status`` is the dict written by a previous boot (see
    ``hardware_info.read_display_status``), or None when absent or unparseable.

    Returns None -- meaning "no hint" -- for anything not positively usable. A
    boot where no driver drove the panel records ``active_controller: None``,
    and trusting that would pin the board to a driver already known to fail. The
    remaining rejections are defensive: this runs before the splash screen, on a
    file written by a separate process that a power cut can tear mid-write, so a
    malformed value must degrade to the shipped order rather than raise.
    """
    if not isinstance(status, dict) or not status.get("initialized"):
        return None
    controller = status.get("active_controller")
    if controller in DEFAULT_CONTROLLER_ORDER:
        return controller
    return None


def controller_order(hint: Optional[str]) -> tuple:
    """Return ``(first, second)`` controllers to probe, most likely first.

    Both controllers are always present, so a wrong hint always has somewhere to
    fall through to. Any value other than a recognized controller yields the
    shipped default, leaving behavior unchanged for boards with no history.
    """
    if hint == CONTROLLER_SSD1680:
        return (CONTROLLER_SSD1680, CONTROLLER_UC8151D)
    return DEFAULT_CONTROLLER_ORDER


def should_attempt_alt(primary: DisplayAttempt, hinted: bool = False) -> bool:
    """Whether to try the second controller after the first attempt.

    Without a hint, True only when the primary failed *by BUSY timeout* (the
    V1-panel signature), so an unrelated fault is not masked by a pointless
    second init. Deliberately independent of the ``il3820`` opt-in: the SSD1680
    fallback is automatic; the opt-in only configures the driver once selected.

    ``hinted`` marks a probe order taken from the previous boot's persisted
    outcome rather than the shipped default. Any failure then falls through,
    because the driver was chosen from unverified disk state and the likeliest
    cause is a stale hint (panel swap, restored config) -- a case the other
    controller does fix. Applying the strict gate there would strand a swapped
    panel on a blank screen with no path back.
    """
    if primary.ok:
        return False
    return True if hinted else primary.busy_timeout


def resolve_outcome(
    primary: DisplayAttempt,
    alt: Optional[DisplayAttempt] = None,
    order: Optional[tuple] = None,
    prior_busy_timeout: bool = False,
) -> DisplayOutcome:
    """Fold the attempt results into the final :class:`DisplayOutcome`.

    Args:
        primary: the attempt against ``order[0]`` (always made).
        alt: the attempt against ``order[1]``, present iff
            :func:`should_attempt_alt` was True and ``main`` ran it. Passing
            ``alt`` when it should not have been attempted is ignored.
        order: the probe order from :func:`controller_order`; defaults to
            :data:`DEFAULT_CONTROLLER_ORDER`.
        prior_busy_timeout: the previous boot's ``busy_timeout``, used only when
            a hinted order meant the UC8151D was never probed this boot.

    ``busy_timeout`` reports whether the panel showed the V1 BUSY-timeout
    signature, which only the UC8151D driver can produce. When a hint skipped
    that driver entirely, the flag is neither True nor False by observation, so
    the previous boot's value carries forward: it gates the web UI's display
    tuning card, and letting it drop to False would hide that card from exactly
    the V1 boards the hint optimizes -- the fix breaking the UI by working.
    """
    first, second = order if order is not None else DEFAULT_CONTROLLER_ORDER
    hinted = first != CONTROLLER_UC8151D

    # Locate the UC8151D attempt, if it ran at all; only it can observe the
    # BUSY-timeout signature.
    if first == CONTROLLER_UC8151D:
        uc_attempt = primary
    elif second == CONTROLLER_UC8151D:
        uc_attempt = alt
    else:
        uc_attempt = None
    busy_timeout = (
        uc_attempt.busy_timeout if uc_attempt is not None else prior_busy_timeout
    )

    if primary.ok:
        return DisplayOutcome(
            initialized=True,
            busy_timeout=busy_timeout,
            active_controller=first,
            error=None,
        )

    if should_attempt_alt(primary, hinted=hinted):
        if alt is not None and alt.ok:
            return DisplayOutcome(
                initialized=True,
                busy_timeout=busy_timeout,
                active_controller=second,
                error=None,
            )
        return DisplayOutcome(
            initialized=False,
            busy_timeout=busy_timeout,
            active_controller=None,
            error=(alt.error if alt is not None else primary.error),
        )

    return DisplayOutcome(
        initialized=False,
        busy_timeout=busy_timeout,
        active_controller=None,
        error=primary.error,
    )


# Labels for the Settings diagnostics event log. Keys are CONTROLLER_*.
PANEL_EVENT_LABEL = {
    CONTROLLER_UC8151D: "UC8151D (V2)",
    CONTROLLER_SSD1680: "SSD1680 (V1)",
}

EVENT_CATEGORY = "display"


def event_log_entry(
    outcome: DisplayOutcome,
    primary: DisplayAttempt,
    alt: Optional[DisplayAttempt] = None,
    order: Optional[tuple] = None,
) -> tuple[str, str]:
    """Return ``(level, message)`` for the Settings diagnostics event log.

    One line per boot so an operator can see, without SSH, which panel
    initialized, that none did, or the non-timeout init error (missing
    overlay, gpiochip/spidev permissions, SPI open) that skipped the other
    driver. ``level`` is one of the event-log severities (info/warning/error).
    """
    first, second = order if order is not None else DEFAULT_CONTROLLER_ORDER
    if outcome.initialized:
        label = PANEL_EVENT_LABEL.get(
            outcome.active_controller, outcome.active_controller or "unknown"
        )
        return "info", f"E-paper panel detected: {label}"

    error = outcome.error or primary.error or "unknown error"
    timed_out = primary.busy_timeout or (alt is not None and alt.busy_timeout)
    if not timed_out:
        probed = first if alt is None else f"{first}, then {second}"
        return "error", f"E-paper init failed ({probed}): {error}"

    parts = [f"{first}: {primary.error or 'failed'}"]
    if alt is not None:
        parts.append(f"{second}: {alt.error or 'failed'}")
    else:
        parts.append(f"{second}: not tried")
    return "warning", "No e-paper panel detected (" + "; ".join(parts) + ")"

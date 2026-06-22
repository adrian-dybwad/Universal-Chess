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


def should_attempt_alt(primary: DisplayAttempt) -> bool:
    """Whether to try the SSD1680 driver after the primary attempt.

    True only when the primary failed *by BUSY timeout* (the V1-panel
    signature). Deliberately independent of the ``il3820`` opt-in: the SSD1680
    fallback is automatic; the opt-in only configures the driver once selected.
    """
    return (not primary.ok) and primary.busy_timeout


def resolve_outcome(
    primary: DisplayAttempt,
    alt: Optional[DisplayAttempt] = None,
) -> DisplayOutcome:
    """Fold the attempt results into the final :class:`DisplayOutcome`.

    Args:
        primary: the UC8151D attempt (always made).
        alt: the SSD1680 attempt, present iff :func:`should_attempt_alt` was True
            and ``main`` ran it. Passing ``alt`` when it should not have been
            attempted is ignored.
    """
    if primary.ok:
        return DisplayOutcome(
            initialized=True,
            busy_timeout=False,
            active_controller=CONTROLLER_UC8151D,
            error=None,
        )

    # Primary failed. The BUSY-timeout flag drives the UI opt-in regardless of
    # whether the alt path succeeds.
    if should_attempt_alt(primary):
        if alt is not None and alt.ok:
            return DisplayOutcome(
                initialized=True,
                busy_timeout=primary.busy_timeout,
                active_controller=CONTROLLER_SSD1680,
                error=None,
            )
        return DisplayOutcome(
            initialized=False,
            busy_timeout=primary.busy_timeout,
            active_controller=None,
            error=(alt.error if alt is not None else primary.error),
        )

    return DisplayOutcome(
        initialized=False,
        busy_timeout=primary.busy_timeout,
        active_controller=None,
        error=primary.error,
    )

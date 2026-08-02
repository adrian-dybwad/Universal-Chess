"""Read apt's machine-readable progress stream during a dependency install.

The dependency step used to run with its output captured and thrown away, so the
one part of an engine install that can spend minutes downloading and unpacking
hundreds of megabytes showed a motionless bar and no text at all. It then failed
against a fixed budget with "Command timed out" and nothing to say what it had
been doing.

apt already publishes exactly what is needed through ``APT::Status-Fd``, the
interface its graphical frontends consume. Each report is::

    <kind>:<subject>:<percent>:<message>

``dlstatus`` covers fetching, ``pmstatus`` unpacking and configuring, and
``pmerror`` a package failure. The percentages are apt's own measurements across
the whole transaction, so nothing here estimates progress -- it only decides
which slice of the step each measurement belongs to, because a run reports three
independent 0-100% sequences (index refresh, download, unpack) that must add up
to one bar that only moves forwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class AptPhase(Enum):
    """Which step of the dependency helper is running."""

    UPDATING_INDEX = "update"
    INSTALLING = "install"


# Which slice of the whole dependency step each phase occupies, as (start, end).
#
# The boundaries are a fixed allocation rather than a measurement: how long the
# refresh takes relative to the download cannot be known before either has run.
# Each phase's *internal* position within its slice is apt's own percentage, so
# only the weighting is chosen here. Fetching gets the largest share because on a
# constrained board it dominates -- Maia's toolchain is several hundred megabytes
# over the same link that serves the rest of the install.
_PHASE_SLICES = {
    (AptPhase.UPDATING_INDEX, "dlstatus"): (0.00, 0.10),
    (AptPhase.INSTALLING, "dlstatus"): (0.10, 0.55),
    (AptPhase.INSTALLING, "pmstatus"): (0.55, 1.00),
}

# "UC_DEPS_PHASE phase=install" -- published by the helper, which is the only
# thing that knows whether it is refreshing the index or installing. apt itself
# reports both identically.
_PHASE_PATTERN = re.compile(r"^UC_DEPS_PHASE\s+phase=(\w+)\s*$")

# "pmstatus:clang:20.0000:Unpacking clang (1:14.0-55.7~deb12u1)". The message may
# contain colons, so it is captured whole rather than split on the separator.
_STATUS_PATTERN = re.compile(r"^(dlstatus|pmstatus|pmerror):([^:]*):([0-9.]+):(.*)$")


@dataclass(frozen=True)
class AptProgress:
    """A reading of the dependency step.

    ``fraction`` spans the whole step, not the phase, so it can drive the bar
    directly. ``activity`` is apt's own description of what it is doing.
    """

    phase: AptPhase | None
    fraction: float
    activity: str


class AptProgressReader:
    """Folds a dependency install's output into one forward-only reading.

    Only lines apt or the helper emit as progress are claimed; everything else is
    reported as unrecognised so the caller can still show it as plain text. That
    split is what lets the banner display every line the command produces while
    still moving the bar on the ones that carry a measurement.
    """

    def __init__(self) -> None:
        """Start with no phase announced and nothing done."""
        self._phase: AptPhase | None = None
        self._fraction = 0.0
        self._activity = ""

    def read_line(self, line: str) -> bool:
        """Consume one output line; return whether it carried progress."""
        text = line.strip()

        announced = _PHASE_PATTERN.match(text)
        if announced is not None:
            try:
                phase = AptPhase(announced.group(1))
            except ValueError:
                # An unknown phase means this reader and the helper have drifted.
                # Keep the last known phase: a stale step name is a smaller fault
                # than raising inside the callback of a running install.
                return False
            if phase is not self._phase:
                # The previous phase's last description belongs to work that has
                # finished; carrying it into the new phase misreports what is
                # running now (the index refresh's final file, shown as the first
                # thing the install is doing).
                self._activity = ""
            self._phase = phase
            return True

        report = _STATUS_PATTERN.match(text)
        if report is None:
            return False
        kind, _subject, percent_text, message = report.groups()
        try:
            percent = float(percent_text)
        except ValueError:
            return False

        self._activity = message.strip()
        if kind == "pmerror":
            # An error is worth showing but says nothing about how far along the
            # transaction is, so it must not move the bar.
            return True

        slice_bounds = _PHASE_SLICES.get((self._phase, kind))
        if slice_bounds is None:
            return True
        start, end = slice_bounds
        within = max(0.0, min(percent / 100.0, 1.0))
        # Never retreat: the phases run in order but each restarts its own count,
        # and a bar that goes backwards reads as the step starting over.
        self._fraction = max(self._fraction, start + (end - start) * within)
        return True

    def progress(self) -> AptProgress:
        """Report the current reading for the whole dependency step."""
        return AptProgress(
            phase=self._phase, fraction=self._fraction, activity=self._activity
        )

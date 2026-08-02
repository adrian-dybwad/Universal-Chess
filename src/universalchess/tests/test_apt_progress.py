"""Tests for parsing apt's machine-readable progress stream.

The dependency install was the one long step with no visibility at all: its
output was captured and discarded until the command returned, so a Maia install
showed a frozen bar for minutes and then failed with "Command timed out". These
tests pin the parsing that turns apt's own progress reports into something the
install banner can show.

The sample lines are real ``APT::Status-Fd`` output, which is the interface apt
provides for exactly this purpose (it is what graphical apt frontends consume).
"""

from __future__ import annotations

import pytest

from universalchess.services.apt_progress import AptPhase, AptProgressReader

# Real APT::Status-Fd lines. Format is "<kind>:<subject>:<percent>:<message>".
_DL_START = "dlstatus:1:0.0000:Retrieving file 1 of 12"
_DL_HALF = "dlstatus:6:50.0000:Retrieving file 6 of 12"
_DL_END = "dlstatus:12:100.0000:Retrieving file 12 of 12"
_PM_UNPACK = "pmstatus:clang:20.0000:Unpacking clang (1:14.0-55.7~deb12u1)"
_PM_CONFIG = "pmstatus:clang:60.0000:Configuring clang (1:14.0-55.7~deb12u1)"
_PM_DONE = "pmstatus:clang:100.0000:Installed clang"


class TestPhaseTracking:
    """Which of the helper's steps is running."""

    def test_no_phase_before_the_helper_announces_one(self):
        """Nothing is claimed until the helper says which step it is on.

        Why this test exists: the reader is fed every line the helper prints,
        including its own logging. Guessing a phase from unrelated text would put
        a wrong step name in front of the user.

        How a regression manifests: the banner names a step that is not running.
        """
        progress = AptProgressReader().progress()

        assert progress.phase is None
        assert progress.fraction == 0.0

    @pytest.mark.parametrize(("line", "expected"), [
        ("UC_DEPS_PHASE phase=update", AptPhase.UPDATING_INDEX),
        ("UC_DEPS_PHASE phase=install", AptPhase.INSTALLING),
    ])
    def test_announced_phase_is_recognised(self, line, expected):
        """Each announced phase is understood.

        Why this test exists: apt reports 0-100% separately for the index refresh
        and for the install, so without knowing which is running the two look like
        one bar that completes and then restarts.

        How a regression manifests: the bar runs to full during the index refresh
        and then jumps backwards when the real install begins.
        """
        reader = AptProgressReader()

        assert reader.read_line(line) is True
        assert reader.progress().phase is expected

    def test_entering_a_phase_drops_the_previous_phases_description(self):
        """A new phase starts with no description of its own yet.

        Why this test exists: the index refresh ends on a description like
        "Retrieving file 4 of 4". Carrying that into the install phase makes the
        banner announce finished work as the first thing the install is doing.

        How a regression manifests: the last line of one phase is shown again as
        the opening line of the next.
        """
        reader = AptProgressReader()
        reader.read_line("UC_DEPS_PHASE phase=update")
        reader.read_line(_DL_END)

        reader.read_line("UC_DEPS_PHASE phase=install")

        assert reader.progress().activity == ""

    def test_an_unknown_phase_is_ignored(self):
        """An unrecognised phase name leaves the previous phase in place.

        Why this test exists: the sentinel is a contract between two files that can
        drift. A phase this reader does not know must not blank out the display or
        raise inside the line callback and abort a healthy install.

        How a regression manifests: a KeyError escapes the callback and the install
        fails at the dependency step for a cosmetic reason.
        """
        reader = AptProgressReader()
        reader.read_line("UC_DEPS_PHASE phase=install")

        assert reader.read_line("UC_DEPS_PHASE phase=teleport") is False
        assert reader.progress().phase is AptPhase.INSTALLING


class TestProgressReadings:
    """The fraction and description shown while apt works."""

    def test_download_percent_is_reported_with_its_description(self):
        """A download reading carries apt's own percent and message.

        Why this test exists: this is the payload the user actually reads. apt
        already computes the percentage across the whole fetch, so it is measured
        rather than modelled, and the message names the file being retrieved.

        How a regression manifests: the banner shows a step name with no detail,
        which is what made the dependency install look frozen.
        """
        reader = AptProgressReader()
        reader.read_line("UC_DEPS_PHASE phase=install")

        assert reader.read_line(_DL_HALF) is True
        progress = reader.progress()
        assert progress.activity == "Retrieving file 6 of 12"
        assert progress.phase is AptPhase.INSTALLING

    def test_unpacking_outranks_downloading_in_the_bar(self):
        """Install-phase readings sit above download readings in the same step.

        Why this test exists: apt downloads everything and then unpacks it, each
        reporting its own 0-100%. Mapping both onto the same range makes the bar
        run to the top and start again; the two must occupy consecutive slices.

        How a regression manifests: the dependency bar completes twice.
        """
        reader = AptProgressReader()
        reader.read_line("UC_DEPS_PHASE phase=install")
        reader.read_line(_DL_END)
        downloaded = reader.progress().fraction
        reader.read_line(_PM_UNPACK)

        assert reader.progress().fraction > downloaded

    def test_the_fraction_never_goes_backwards(self):
        """Progress is monotonic across every phase transition.

        Why this test exists: three separate 0-100% sequences feed this one bar
        (index refresh, download, unpack). Any of the boundaries between them can
        send it backwards, and a bar that retreats reads as a restart.

        How a regression manifests: the percentage drops at a phase boundary.
        """
        reader = AptProgressReader()
        readings = []
        for line in (
            "UC_DEPS_PHASE phase=update",
            _DL_START, _DL_HALF, _DL_END,
            "UC_DEPS_PHASE phase=install",
            _DL_START, _DL_HALF, _DL_END,
            _PM_UNPACK, _PM_CONFIG, _PM_DONE,
        ):
            reader.read_line(line)
            readings.append(reader.progress().fraction)

        assert readings == sorted(readings), readings
        assert readings[-1] == pytest.approx(1.0)

    def test_ordinary_apt_chatter_is_not_mistaken_for_progress(self):
        """Lines that are not status reports are left for the caller to display.

        Why this test exists: the helper's own log lines and apt's human output
        share the stream with the status reports. Treating them as progress would
        either move the bar on no evidence or swallow text the user should see.

        How a regression manifests: the bar jumps on unrelated output, or useful
        lines never reach the banner because the reader claimed them.
        """
        reader = AptProgressReader()
        for line in (
            "uc-engine-deps: installing: clang meson",
            "Reading package lists...",
            "Get:1 http://deb.debian.org/debian bookworm/main armhf clang 1234 kB",
        ):
            assert reader.read_line(line) is False

    def test_a_malformed_status_line_is_ignored(self):
        """A truncated or non-numeric status line changes nothing.

        Why this test exists: this parser runs inside the line callback of a live
        install. A ValueError from a partial line written during a flush would end
        the install at the dependency step.

        How a regression manifests: the install fails with a parsing error instead
        of installing packages.
        """
        reader = AptProgressReader()
        reader.read_line("UC_DEPS_PHASE phase=install")
        reader.read_line(_DL_HALF)
        before = reader.progress().fraction
        for line in ("pmstatus:clang:notanumber:x", "dlstatus:1", "pmstatus::"):
            assert reader.read_line(line) is False

        assert reader.progress().fraction == before

    def test_an_apt_error_is_surfaced_as_the_activity(self):
        """apt's own error report becomes the visible activity.

        Why this test exists: pmerror carries the actionable failure text. Dropping
        it leaves the user with a generic "could not install dependencies" while
        apt's specific reason is discarded.

        How a regression manifests: package failures lose their cause.
        """
        reader = AptProgressReader()
        reader.read_line("UC_DEPS_PHASE phase=install")

        recognised = reader.read_line(
            "pmerror:clang:0.0000:unable to install: no space left on device"
        )

        assert recognised is True
        assert "no space left on device" in reader.progress().activity

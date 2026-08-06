"""Tests for the postinst's removal of the serial console from cmdline.txt.

The DGT Centaur board is wired to the Pi's primary UART, so a kernel serial
console on that same UART puts a login getty in contention with the board. The
postinst therefore has to strip any ``console=serial0`` token before the board
software runs.

These guard the regression where the strip was written as a positional delete::

    REPLY=$(sed 's/[^ ]* *//' $CMDLINEFILE)

That deletes the *first whitespace-delimited token, whatever it is*, while the
surrounding ``if`` only checks that ``console=serial0`` appears *somewhere* on
the line. It worked solely because Raspberry Pi Imager happens to write the
console token first.

Raspberry Pi OS does not keep it there. ``raspi-config``'s enable path inserts
the token immediately before ``root=``::

    sed -i $CMDLINE -e "s/root=/console=serial0,115200 root=/"

and cloud-init drives exactly that path whenever user-data sets
``rpi.interfaces.serial``. On such a card the first token is ``console=tty1``,
so the old code deleted the tty1 console and left the serial console in place --
the precise opposite of its purpose, with no error and no output to reveal it.

The tests execute the shipped shell function rather than pattern-matching it, so
the behaviour cannot drift from the script that runs on the board.
"""

import re
import subprocess
from pathlib import Path

import pytest

import universalchess.services.update_service as us

# Repo layout: .../src/universalchess/services/update_service.py
# -> repo root is four parents up, then packaging/deb-root/DEBIAN/postinst.
POSTINST = (
    Path(us.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)

STRIP_FUNCTION = "strip_serial_console"

# The console tokens that contend with the Centaur board. serial0 is the udev
# alias; on a Pi Zero / Zero 2 W it resolves to ttyS0, and ttyAMA0 is what
# raspi-config writes when no serial0 alias exists.
CONTENDING_CONSOLES = ["serial0", "ttyS0", "ttyAMA0"]

ROOT_ARGS = "root=PARTUUID=4f2c9ea0-02 rootfstype=ext4 fsck.repair=yes rootwait"


@pytest.fixture
def strip_console(tmp_path):
    """Return a callable that runs the shipped strip function on given text.

    The function definition is lifted verbatim out of the postinst and sourced,
    so these tests exercise the real implementation.
    """
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    text = POSTINST.read_text()
    match = re.search(rf"(?sm)^{STRIP_FUNCTION}\(\) \{{.*?^\}}", text)
    assert match, f"{STRIP_FUNCTION} not found in postinst"
    function_source = match.group(0)

    counter = {"n": 0}

    def run(cmdline: str) -> str:
        counter["n"] += 1
        target = tmp_path / f"cmdline{counter['n']}.txt"
        target.write_text(cmdline)
        proc = subprocess.run(  # noqa: S603 - runs the postinst's own function
            ["/bin/sh", "-c", f"{function_source}\n{STRIP_FUNCTION} \"$1\"", "sh", str(target)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return target.read_text()

    return run


class TestSerialConsoleRemoval:
    @pytest.mark.parametrize("console", CONTENDING_CONSOLES)
    def test_removes_the_console_token_when_it_is_not_first(self, strip_console, console):
        """A console token positioned before root= must still be removed.

        Why this test exists: this is the exact layout raspi-config produces,
        and the positional `sed 's/[^ ]* *//'` could not handle it.

        How the regression manifests: console=<serial device> survives in the
        output while console=tty1 -- the first token -- is deleted instead.
        """
        result = strip_console(f"console=tty1 console={console},115200 {ROOT_ARGS}\n")

        assert f"console={console}" not in result
        assert "console=tty1" in result
        assert ROOT_ARGS in result

    @pytest.mark.parametrize("console", CONTENDING_CONSOLES)
    def test_removes_the_console_token_when_it_is_first(self, strip_console, console):
        """The Imager layout must keep working.

        Why this test exists: the old code only ever handled this case, so a fix
        that regressed it would break every card written by Raspberry Pi Imager.

        How the regression manifests: the console token survives at the head of
        the line, leaving a getty on the Centaur's UART.
        """
        result = strip_console(f"console={console},115200 console=tty1 {ROOT_ARGS}\n")

        assert f"console={console}" not in result
        assert "console=tty1" in result
        assert ROOT_ARGS in result

    def test_removes_the_console_token_when_it_is_last(self, strip_console):
        """A trailing console token must be removed.

        Why this test exists: raspi-config's own disable uses
        `s/console=serial0,[0-9]\\+ //`, which requires a trailing space and so
        silently leaves a token that ends the line. Copying that idiom verbatim
        would inherit the hole.

        How the regression manifests: the token survives when nothing follows
        it.
        """
        result = strip_console(f"{ROOT_ARGS} console=serial0,115200\n")

        assert "console=serial0" not in result
        assert ROOT_ARGS in result

    def test_removes_a_console_token_that_has_no_baud_rate(self, strip_console):
        """`console=serial0` with no baud suffix must be removed.

        Why this test exists: the baud rate is optional in the kernel's console
        syntax, and a pattern requiring `,[0-9]+` skips the bare form entirely
        while the enclosing grep still reports a match.

        How the regression manifests: a bare console=serial0 survives.
        """
        result = strip_console(f"console=serial0 {ROOT_ARGS}\n")

        assert "console=serial0" not in result
        assert ROOT_ARGS in result

    def test_removes_every_occurrence(self, strip_console):
        """Duplicated console tokens must all go.

        Why this test exists: repeated enable/disable cycles by raspi-config and
        the Imager can leave two console tokens on one line. A non-global
        substitution removes one and leaves the board still contended, which
        looks like the fix simply did not work.

        How the regression manifests: one console=serial0 remains.
        """
        result = strip_console(
            f"console=serial0,115200 console=tty1 console=serial0,115200 {ROOT_ARGS}\n"
        )

        assert "console=serial0" not in result

    def test_preserves_a_line_with_no_serial_console(self, strip_console):
        """The no-op case must change nothing. This is the empty/absent case.

        Why this test exists: an over-broad pattern (for example one keying on
        `console=` alone) would strip console=tty1 and drop the operator's only
        working console on a board with no network.

        How the regression manifests: any difference at all from the input.
        """
        original = f"console=tty1 {ROOT_ARGS}\n"

        assert strip_console(original) == original

    def test_is_idempotent(self, strip_console):
        """Running twice must equal running once.

        Why this test exists: postinst runs on every package upgrade, so a
        function that shaves a token per invocation would progressively destroy
        cmdline.txt -- eventually taking root= with it and leaving an unbootable
        card.

        How the regression manifests: the second pass differs from the first.
        """
        once = strip_console(f"console=tty1 console=serial0,115200 {ROOT_ARGS}\n")

        assert strip_console(once) == once

    def test_does_not_disturb_other_kernel_arguments(self, strip_console):
        """Neighbouring arguments must survive byte-for-byte.

        Why this test exists: cmdline.txt is a single line where every token
        matters; root= or the cloud-init datasource being clipped by a greedy
        pattern produces a card that fails to boot or silently skips its
        first-boot configuration.

        How the regression manifests: any token other than the console one is
        altered or the tokens are re-spaced.
        """
        datasource = "ds=nocloud;i=rpi-imager-1785934397333"
        result = strip_console(
            f"console=serial0,115200 console=tty1 {ROOT_ARGS} "
            f"cfg80211.ieee80211_regdom=US {datasource}\n"
        )

        assert result.split() == [
            "console=tty1",
            *ROOT_ARGS.split(),
            "cfg80211.ieee80211_regdom=US",
            datasource,
        ]

    def test_leaves_a_single_line(self, strip_console):
        """The result must remain one line.

        Why this test exists: the old code piped through `echo -e`, which
        interprets backslash escapes and would turn a literal `\\n` in a kernel
        argument into a real newline. The bootloader reads only the first line,
        so everything after it would be silently dropped.

        How the regression manifests: more than one non-empty line.
        """
        result = strip_console(f"console=serial0,115200 console=tty1 {ROOT_ARGS}\n")

        assert len([line for line in result.splitlines() if line.strip()]) == 1

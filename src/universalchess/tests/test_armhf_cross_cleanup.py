"""Tests for removing the armhf cross toolchain from boards that cannot use it.

The regression these guard is a board-wide one, not an engine one. The package
declared ``Recommends: gcc-arm-linux-gnueabihf, libc6-dev-armhf-cross`` (apt
installs Recommends by default), but those exist only so an *arm64* host can
build the Centaur display shim for Centaur's 32-bit ABI. On 32-bit ARM they are
never used -- ``centaur-armhf-setup`` exits immediately unless the host is arm64
and the shim is built by the native compiler there.

On an ARMv6 board (Pi Zero W / Pi 1) they are worse than unused. The cross
package installs ARMv7-A startup objects at ``/usr/arm-linux-gnueabihf/lib``,
and that directory precedes ``/usr/lib/arm-linux-gnueabihf`` on the native gcc's
search path, so every locally linked binary picks up ARMv7 ``crt1.o`` and
segfaults before reaching ``main`` on the ARMv6 CPU. Measured on a Pi Zero W:
``int main(void){return 0;}`` exits 139 with the default link and 0 when forced
to the native crt with ``-B/usr/lib/arm-linux-gnueabihf/``. Every engine built
from source on such a board crashes at startup while its binary sits on disk
looking installed.

The first version of this cleanup then caused a worse failure than the one it
fixed, and most of what is pinned below comes from that. It purged
``gcc-arm-linux-gnueabihf`` as well, on the theory that a package named for a
cross triplet cannot be needed on a board that is not arm64. On Raspberry Pi OS
armhf that triplet is the *native* one and ``gcc`` declares
``Depends: gcc-arm-linux-gnueabihf (= 4:14.2.0-1+rpi1)``, so the purge cascaded
through gcc, g++, build-essential and finally universal-chess itself, taking the
board's application down mid-transaction. Only ``libc6-dev-armhf-cross`` is both
unusable and harmful here, and purging it alone removes exactly one package.

The tests run the real helper with fake dpkg/dpkg-query/apt-get on PATH, so the
arch gate, the package set, and the exact removal command are pinned rather than
described.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-drop-armhf-cross"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_POSTINST = _REPO_ROOT / "packaging" / "deb-root" / "DEBIAN" / "postinst"
_CONTROL = _REPO_ROOT / "packaging" / "deb-root" / "DEBIAN" / "control"
_UNIT = (
    _REPO_ROOT / "packaging" / "deb-root" / "etc" / "systemd" / "system"
    / "universal-chess-armhf-cleanup.service"
)

# The two packages the control file used to recommend onto every board. Only the
# second may ever be removed: on an armhf host the first is the native compiler.
CROSS_COMPILER_PKG = "gcc-arm-linux-gnueabihf"
CROSS_LIBC_DEV_PKG = "libc6-dev-armhf-cross"

# A package apt might drag out with the purge. Stands in for the real cascade
# (gcc -> g++ -> build-essential -> universal-chess) that took the board down.
COLLATERAL_PKG = "build-essential"

# Floor for how long the purge may wait on the dpkg lock. Measured on a Pi Zero
# W: the upgrade that starts the cleanup unit still held the lock more than
# three minutes after the unit began, so anything under ten minutes risks
# repeating the failure this floor exists to prevent.
MIN_LOCK_WAIT_SECONDS = 600

# Fake dpkg: reports the architecture the test is simulating.
_FAKE_DPKG = """#!/bin/sh
if [ "$1" = "--print-architecture" ]; then
    echo "$UC_TEST_ARCH"
    exit 0
fi
echo "dpkg $*" >> "$UC_TEST_LOG"
exit 0
"""

# Fake dpkg-query: a package is installed only when named in UC_TEST_INSTALLED.
_FAKE_DPKG_QUERY = """#!/bin/sh
for arg in "$@"; do pkg="$arg"; done
for installed in $UC_TEST_INSTALLED; do
    if [ "$pkg" = "$installed" ]; then
        echo "install ok installed"
        exit 0
    fi
done
exit 1
"""

# Fake apt-get. A `-s` run answers with the removal plan apt would produce: the
# packages it was asked to purge, plus any collateral the test injects via
# UC_TEST_EXTRA_REMOVALS. Anything else is treated as the real purge and honours
# UC_TEST_APT_EXIT.
_FAKE_APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$UC_TEST_LOG"
simulate=no
for arg in "$@"; do
    [ "$arg" = "-s" ] && simulate=yes
done
if [ "$simulate" = yes ]; then
    [ "${UC_TEST_SIM_EXIT:-0}" = "0" ] || exit "$UC_TEST_SIM_EXIT"
    for arg in "$@"; do
        case "$arg" in
            -*|purge) ;;
            *) echo "Purg $arg [1.0]" ;;
        esac
    done
    for extra in ${UC_TEST_EXTRA_REMOVALS:-}; do
        echo "Remv $extra [1.0]"
    done
    exit 0
fi
exit ${UC_TEST_APT_EXIT:-0}
"""


def _write_tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def helper_env(tmp_path):
    """Fake dpkg/dpkg-query/apt-get on PATH plus a call log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "dpkg", _FAKE_DPKG)
    _write_tool(bindir, "dpkg-query", _FAKE_DPKG_QUERY)
    _write_tool(bindir, "apt-get", _FAKE_APT_GET)

    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["UC_TEST_LOG"] = str(log)
    env["UC_TEST_ARCH"] = "armhf"
    env["UC_TEST_INSTALLED"] = ""
    return env, log


def _run(env):
    proc = subprocess.run(  # noqa: S603 - invokes the pinned helper with no arguments
        ["/bin/sh", str(_HELPER)],
        env=env, capture_output=True, text=True,
    )
    log = Path(env["UC_TEST_LOG"])
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _apt_calls(calls):
    return [c for c in calls if c.startswith("apt-get ")]


def _purge_calls(calls):
    """apt-get invocations that actually remove something (not `-s` dry runs)."""
    return [c for c in _apt_calls(calls) if "-s" not in c.split()]


def _simulation_calls(calls):
    return [c for c in _apt_calls(calls) if "-s" in c.split()]


def _lock_wait_seconds(apt_call):
    """Seconds the helper told apt to block on the dpkg lock, or None."""
    match = re.search(r"DPkg::Lock::Timeout=(\d+)", apt_call)
    return int(match.group(1)) if match else None


def _unit_start_timeout_seconds():
    """TimeoutStartSec from the shipped unit, in seconds.

    Only the bare-integer form is accepted: systemd also parses "10min" and
    "1h", but allowing them here would mean reimplementing systemd's time
    parser in a test. The unit is required to state plain seconds instead.
    """
    match = re.search(r"^TimeoutStartSec=(\d+)$", _UNIT.read_text(), re.MULTILINE)
    assert match, "unit must set TimeoutStartSec as a plain number of seconds"
    return int(match.group(1))


def test_helper_exists_and_is_shipped():
    """The helper must ship in the source tree the package installs from.

    Why this test exists: the postinst enables a unit whose ExecStart points at
    this path. A missing file turns self-healing into a unit that fails on every
    boot, which is silent unless someone reads the journal.
    """
    assert _HELPER.is_file(), f"helper missing: {_HELPER}"


class TestArchGate:
    """Only hosts that cannot use the cross toolchain have it removed."""

    def test_keeps_the_toolchain_on_arm64_where_the_shim_needs_it(self, helper_env):
        """An arm64 host must keep both packages.

        Why this test exists: on arm64 the native compiler cannot emit Centaur's
        32-bit armhf ABI, so these packages are how the display shim gets built.
        Removing them there would trade this bug for a broken Centaur import.

        How the regression manifests: an unconditional cleanup calls apt-get
        purge on an arm64 board, so this asserts apt-get is never invoked.
        """
        env, _log = helper_env
        env["UC_TEST_ARCH"] = "arm64"
        env["UC_TEST_INSTALLED"] = f"{CROSS_COMPILER_PKG} {CROSS_LIBC_DEV_PKG}"

        proc, calls = _run(env)

        assert proc.returncode == 0
        assert _apt_calls(calls) == []

    @pytest.mark.parametrize("arch", ["armhf", "armel", "unknown"])
    def test_removes_the_toolchain_on_every_non_arm64_host(self, helper_env, arch):
        """Any host that is not arm64 has no use for these packages.

        Why this test exists: the harm is measurable only on ARMv6, but the
        packages are unused on all 32-bit ARM, and dpkg reports both a Pi Zero
        and a Pi 4 as "armhf" -- there is no architecture token that separates
        them. Gating on "not arm64" matches the guard centaur-armhf-setup
        already uses, so the two cannot disagree about who needs them.

        How the regression manifests: a gate written as `= "armhf"` misses the
        armel and unknown cases, leaving a board unhealed.
        """
        env, _log = helper_env
        env["UC_TEST_ARCH"] = arch
        env["UC_TEST_INSTALLED"] = f"{CROSS_COMPILER_PKG} {CROSS_LIBC_DEV_PKG}"

        proc, calls = _run(env)

        assert proc.returncode == 0
        assert len(_purge_calls(calls)) == 1


class TestRemoval:
    """What the helper actually asks apt to do."""

    def test_never_removes_the_native_compiler_package(self, helper_env):
        """``gcc-arm-linux-gnueabihf`` must survive even when installed.

        Why this test exists: this is the field failure. On Raspberry Pi OS
        armhf that triplet is the native one, and ``gcc`` declares
        ``Depends: gcc-arm-linux-gnueabihf``, so purging it removed gcc, g++,
        build-essential and universal-chess itself -- the cleanup took down the
        application it shipped with, mid-transaction. The package is only ever a
        real cross compiler on arm64, and the arch gate already keeps everything
        there, so no host exists on which this script should remove it.

        How the regression manifests: the package name reappears in the purge
        command, and the next board to run the cleanup loses its toolchain and
        its application.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = f"{CROSS_COMPILER_PKG} {CROSS_LIBC_DEV_PKG}"

        proc, calls = _run(env)

        assert proc.returncode == 0
        purge = _purge_calls(calls)
        assert len(purge) == 1
        assert CROSS_LIBC_DEV_PKG in purge[0]
        assert CROSS_COMPILER_PKG not in purge[0]
        assert " -y" in purge[0]

    def test_touches_nothing_when_only_the_compiler_is_installed(self, helper_env):
        """A board with the compiler but not the cross libc is left alone.

        Why this test exists: the compiler alone is harmless -- it is the native
        one -- and the ARMv7 startup objects come solely from
        libc6-dev-armhf-cross. With nothing removable present the helper must
        not invoke apt at all, rather than running a purge with an empty package
        list (which upgrades or errors depending on the subcommand).

        How the regression manifests: apt-get is called on a board that has
        nothing this script is allowed to remove.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_COMPILER_PKG

        proc, calls = _run(env)

        assert proc.returncode == 0
        assert _apt_calls(calls) == []

    def test_touches_nothing_when_neither_package_is_installed(self, helper_env):
        """The healthy board -- and every boot after the first -- is a no-op.

        Why this test exists: this runs from a boot unit on every start, so the
        steady state must not invoke apt at all. An apt-get run on each boot
        would hold the dpkg lock and race a genuine package operation.

        How the regression manifests: apt-get is called with an empty package
        list, which upgrades or errors depending on the subcommand -- the kind
        of accident that only shows up on a user's board.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = ""

        proc, calls = _run(env)

        assert proc.returncode == 0
        assert _apt_calls(calls) == []

    def test_does_not_let_apt_remove_unrelated_packages(self, helper_env):
        """The removal is scoped to the two named packages.

        Why this test exists: --auto-remove would additionally take out anything
        apt considers orphaned, which on a board carrying hand-installed engine
        build dependencies can cascade well beyond the cross toolchain. The
        cleanup must fix one specific mistake, not garbage-collect the system.

        How the regression manifests: --auto-remove (or --purge of a pattern)
        appears in the command.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = f"{CROSS_COMPILER_PKG} {CROSS_LIBC_DEV_PKG}"

        _proc, calls = _run(env)

        apt = _purge_calls(calls)[0]
        assert "--auto-remove" not in apt
        assert "autoremove" not in apt

    def test_aborts_when_apt_would_remove_anything_else(self, helper_env):
        """A plan with collateral is refused instead of executed.

        Why this test exists: reasoning about which package is a cross package
        is exactly what failed -- the name looked like a cross toolchain and was
        the native compiler. apt is the only thing that knows the real
        dependency graph on a given image, so the helper asks it first and
        stops if the answer names anything it did not. This guard is what keeps
        a future mistake in the package list from being fatal rather than
        merely wrong.

        How the regression manifests: the purge runs anyway and removes
        build-essential (and, behind it, the application), which is precisely
        the outage this replaces.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG
        env["UC_TEST_EXTRA_REMOVALS"] = COLLATERAL_PKG

        proc, calls = _run(env)

        assert proc.returncode != 0
        assert _simulation_calls(calls), "helper must ask apt for the plan first"
        assert _purge_calls(calls) == [], "no removal may run after a bad plan"

    def test_aborts_when_the_plan_cannot_be_obtained(self, helper_env):
        """No plan means no purge, rather than a purge with no information.

        Why this test exists: apt fails to plan on exactly the boards where
        proceeding is most dangerous -- a half-finished transaction, a broken
        dependency state. Treating "could not ask" as "nothing to worry about"
        reinstates the unguarded purge under the one condition that makes it
        most likely to cascade.

        How the regression manifests: the guard reads an empty plan, finds no
        offending package in it, and falls through to the real removal.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG
        env["UC_TEST_SIM_EXIT"] = "100"

        proc, calls = _run(env)

        assert proc.returncode != 0
        assert _purge_calls(calls) == []

    def test_checks_the_plan_before_every_removal(self, helper_env):
        """The dry run happens first, and only then the purge.

        Why this test exists: a guard that runs after the removal, or not at
        all on the common path, protects nothing. Ordering is the whole point,
        so it is asserted rather than assumed.

        How the regression manifests: the purge is recorded before the -s call,
        or there is no -s call at all on the path that does remove something.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG

        proc, calls = _run(env)

        assert proc.returncode == 0
        simulation_index = calls.index(_simulation_calls(calls)[0])
        purge_index = calls.index(_purge_calls(calls)[0])
        assert simulation_index < purge_index

    def test_reports_failure_when_apt_cannot_remove_them(self, helper_env):
        """A failed removal exits non-zero instead of claiming success.

        Why this test exists: the board is still broken if the purge failed, and
        the unit's state is the only signal anyone will see. Swallowing the
        error would leave a board that reports healthy and still segfaults every
        engine it builds.

        How the regression manifests: exit 0 after a failed apt-get, so
        `systemctl status` shows success on a board that was not fixed.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG
        env["UC_TEST_APT_EXIT"] = "100"

        proc, _calls = _run(env)

        assert proc.returncode != 0

    def test_asks_apt_to_wait_out_the_in_flight_dpkg_transaction(self, helper_env):
        """The purge blocks on the dpkg lock instead of polling for it.

        Why this test exists: the postinst starts this unit from inside its own
        apt transaction, so the lock is always held when the helper begins.
        The first implementation polled fuser for a bounded 120 s and then ran
        the purge regardless. On a Pi Zero W the installing transaction was
        still going three minutes in, so the purge lost the race with the very
        upgrade shipping it -- "Could not get lock /var/lib/dpkg/lock-frontend.
        It is held by process (apt-get)" -- and the board stayed broken.
        Handing the wait to apt removes both the too-short bound and the
        check-then-act gap between observing a free lock and taking it.

        How the regression manifests: no DPkg::Lock::Timeout on the command, so
        the purge fails the instant it collides with the upgrade; or a timeout
        short enough to expire on a slow board, which is the original bug.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG

        proc, calls = _run(env)

        assert proc.returncode == 0
        wait_seconds = _lock_wait_seconds(_purge_calls(calls)[0])
        assert wait_seconds is not None, "purge must ask apt to wait for the lock"
        assert wait_seconds >= MIN_LOCK_WAIT_SECONDS


class TestPackagingWiring:
    """The package must stop causing the problem and must run the cleanup."""

    def test_control_no_longer_recommends_the_cross_toolchain(self):
        """The deb must not pull these packages onto every board.

        Why this test exists: this is the root cause. apt installs Recommends by
        default, so the line put an ARMv7 crt on every ARMv6 board the package
        was installed on. Re-adding it would silently re-break every Pi Zero,
        and the cleanup unit would then fight the package manager.

        How the regression manifests: either package name reappears in a
        Recommends/Depends field of the control file.
        """
        control = _CONTROL.read_text()
        dependency_fields = [
            line for line in control.splitlines()
            if line.startswith(("Depends:", "Recommends:", "Suggests:", "Pre-Depends:"))
        ]
        for line in dependency_fields:
            assert CROSS_COMPILER_PKG not in line, line
            assert CROSS_LIBC_DEV_PKG not in line, line

    def test_cleanup_unit_runs_the_helper(self):
        """The shipped unit must invoke the helper at its installed path.

        Why this test exists: the unit and the helper are shipped by different
        parts of the tree (etc/ vs scripts/), so a rename on one side is not
        caught by anything else. A wrong ExecStart fails only on the board.
        """
        unit = _UNIT.read_text()
        assert f"ExecStart=/opt/universalchess/scripts/{_HELPER.name}" in unit
        assert "Type=oneshot" in unit

    def test_unit_outlasts_the_time_the_helper_spends_waiting(self, helper_env):
        """systemd must not kill the helper while it waits for the lock.

        Why this test exists: TimeoutStartSec bounds the whole ExecStart, and
        nearly all of that budget goes on waiting for the installing
        transaction to finish. A unit timeout at or below the lock wait means
        systemd SIGTERMs the helper on exactly the slow boards the wait was
        lengthened for -- and if it lands mid-purge, dpkg is left interrupted
        and needs `dpkg --configure -a` by hand before anything installs again.

        How the regression manifests: the two numbers are edited independently
        and the unit's timeout is left behind, so the cleanup dies before apt
        ever gets the lock.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG

        _proc, calls = _run(env)

        wait_seconds = _lock_wait_seconds(_purge_calls(calls)[0])
        # The purge itself still has to run after the wait ends, so the unit
        # needs headroom beyond the wait rather than merely matching it.
        assert _unit_start_timeout_seconds() > wait_seconds

    def test_postinst_enables_and_starts_the_cleanup(self):
        """The postinst must both enable the unit and start it now.

        Why this test exists: enabling alone defers the fix to the next reboot,
        and an upgrade does not reboot -- so a user who updates and immediately
        installs an engine would still get a segfaulting binary. Starting must
        be non-blocking because the helper waits for the dpkg lock that this
        very postinst is holding; a blocking start would deadlock the install.

        How the regression manifests: a missing --no-block hangs the upgrade
        until the helper's wait expires; a missing start delays the repair.
        """
        postinst = _POSTINST.read_text()
        assert "universal-chess-armhf-cleanup.service" in postinst
        assert "systemctl enable universal-chess-armhf-cleanup.service" in postinst
        assert (
            "systemctl start --no-block universal-chess-armhf-cleanup.service"
            in postinst
        )

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

The tests run the real helper with fake dpkg/dpkg-query/apt-get on PATH, so the
arch gate and the exact removal command are pinned rather than described.
"""

import os
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

# The two packages the control file used to recommend onto every board.
CROSS_COMPILER_PKG = "gcc-arm-linux-gnueabihf"
CROSS_LIBC_DEV_PKG = "libc6-dev-armhf-cross"

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

_FAKE_APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$UC_TEST_LOG"
exit ${UC_TEST_APT_EXIT:-0}
"""

# Fake fuser: reports the dpkg frontend lock held until a counter file says
# otherwise, so the helper's wait loop can be observed rather than assumed.
_FAKE_FUSER = """#!/bin/sh
count=0
[ -f "$UC_TEST_FUSER_COUNT" ] && count=$(cat "$UC_TEST_FUSER_COUNT")
count=$((count + 1))
echo "$count" > "$UC_TEST_FUSER_COUNT"
echo "fuser $*" >> "$UC_TEST_LOG"
# Non-zero means "no process holds it", which releases the helper's wait.
[ "$count" -le "${UC_TEST_LOCK_HELD_TIMES:-0}" ]
"""


def _write_tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def helper_env(tmp_path):
    """Fake dpkg/dpkg-query/apt-get/fuser on PATH plus a call log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "dpkg", _FAKE_DPKG)
    _write_tool(bindir, "dpkg-query", _FAKE_DPKG_QUERY)
    _write_tool(bindir, "apt-get", _FAKE_APT_GET)
    _write_tool(bindir, "fuser", _FAKE_FUSER)

    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["UC_TEST_LOG"] = str(log)
    env["UC_TEST_ARCH"] = "armhf"
    env["UC_TEST_INSTALLED"] = ""
    env["UC_TEST_FUSER_COUNT"] = str(tmp_path / "fuser.count")
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
        assert len(_apt_calls(calls)) == 1


class TestRemoval:
    """What the helper actually asks apt to do."""

    def test_removes_both_packages_in_one_transaction(self, helper_env):
        """Both packages are purged together, non-interactively.

        Why this test exists: the cross compiler depends on the cross libc, so
        removing them in separate transactions makes the first one's outcome
        depend on ordering. One command with both named is unambiguous, and -y
        matters because this runs unattended from a boot unit with no terminal.

        How the regression manifests: two apt-get lines instead of one, or a
        missing -y that leaves the unit hanging on a confirmation prompt.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = f"{CROSS_COMPILER_PKG} {CROSS_LIBC_DEV_PKG}"

        proc, calls = _run(env)

        apt = _apt_calls(calls)
        assert proc.returncode == 0
        assert len(apt) == 1
        assert CROSS_COMPILER_PKG in apt[0]
        assert CROSS_LIBC_DEV_PKG in apt[0]
        assert " -y" in apt[0]

    def test_removes_only_the_package_that_is_present(self, helper_env):
        """A partially-installed pair names only what is actually installed.

        Why this test exists: naming an absent package makes apt exit non-zero
        on some versions, which would turn a successful cleanup into a unit
        failure and stop the board from reporting itself healed.

        How the regression manifests: the apt line carries both names when only
        one is installed.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG

        proc, calls = _run(env)

        apt = _apt_calls(calls)
        assert proc.returncode == 0
        assert len(apt) == 1
        assert CROSS_LIBC_DEV_PKG in apt[0]
        assert CROSS_COMPILER_PKG not in apt[0]

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

        apt = _apt_calls(calls)[0]
        assert "--auto-remove" not in apt
        assert "autoremove" not in apt

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

    def test_waits_for_an_in_flight_dpkg_transaction(self, helper_env):
        """The purge waits until the dpkg frontend lock is free.

        Why this test exists: the postinst starts this unit while its own dpkg
        transaction still holds the lock, which is exactly why the cleanup is a
        unit rather than an inline postinst step. Without the wait the first
        run -- the one that matters, immediately after the upgrade that ships
        this fix -- fails to acquire the lock and the board stays broken until
        the next boot.

        How the regression manifests: apt-get is invoked before fuser reports
        the lock free, so the recorded call order has apt-get first.
        """
        env, _log = helper_env
        env["UC_TEST_INSTALLED"] = CROSS_LIBC_DEV_PKG
        env["UC_TEST_LOCK_HELD_TIMES"] = "1"

        proc, calls = _run(env)

        assert proc.returncode == 0
        fuser_calls = [i for i, c in enumerate(calls) if c.startswith("fuser ")]
        apt_index = next(i for i, c in enumerate(calls) if c.startswith("apt-get "))
        # The lock was reported held once, so the helper must have polled at
        # least twice and only then purged.
        assert len(fuser_calls) >= 2
        assert apt_index > max(fuser_calls)


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

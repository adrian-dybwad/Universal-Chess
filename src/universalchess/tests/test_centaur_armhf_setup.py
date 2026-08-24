"""Tests for ``scripts/centaur-armhf-setup``.

Centaur is a 32-bit armhf ELF. On Raspberry Pi OS 64-bit the kernel sets
``CONFIG_COMPAT=y``, so ``libc6:armhf`` is enough for the process to run.
The Orange Pi Zero 2W Armbian ``sunxi64`` kernel was measured with
``# CONFIG_COMPAT is not set``: AArch32 userland cannot execute, and
``libc6:armhf`` alone leaves ``centaur`` as ``Exec format error``. That host
needs ``qemu-user-static`` (binfmt) as well.

Which host is which is decided by *running* an AArch32 binary, not by reading
``CONFIG_COMPAT`` out of a kernel config file. An earlier version read
``/proc/config.gz`` and treated an unreadable config as "no COMPAT". Raspberry
Pi OS does not enable ``CONFIG_IKCONFIG_PROC``, so that file is absent there and
every 64-bit Pi doing a Centaur import would have installed qemu-user-static --
the exact outcome two of the tests below forbid. The probe runs the armhf
loader instead and only treats shell status 126 (ENOEXEC) as "cannot exec".

The helper's grant is a closed package set. These tests drive the real script
with fake ``dpkg`` / ``dpkg-query`` / ``apt-get`` / ``update-binfmts`` on PATH,
plus a fake armhf loader, so the arch gate, the qemu decision, the apt argv,
and the qemu-arm binfmt registration stay pinned. Raspberry Pi OS 64-bit
fixtures must never call update-binfmts: registering qemu-arm there would
intercept native AArch32.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "centaur-armhf-setup"

ARMHF_RUNTIME_PKG = "libc6:armhf"
CROSS_PACKAGES = ("gcc-arm-linux-gnueabihf", "libc6-dev-armhf-cross")
QEMU_PKG = "qemu-user-static"
ALL_ARMHF = (ARMHF_RUNTIME_PKG, *CROSS_PACKAGES)

# What a POSIX shell returns when execve refuses a file that does exist.
EXEC_FORMAT_ERROR_STATUS = 126

_FAKE_DPKG = """#!/bin/sh
echo "dpkg $*" >> "$UC_TEST_LOG"
if [ "$1" = "--print-architecture" ]; then
    echo "${UC_TEST_ARCH:-arm64}"
    exit 0
fi
if [ "$1" = "--print-foreign-architectures" ]; then
    [ -n "${UC_TEST_FOREIGN_ARCH:-}" ] && echo "$UC_TEST_FOREIGN_ARCH"
    exit 0
fi
if [ "$1" = "--add-architecture" ]; then
    exit 0
fi
exit 0
"""

_FAKE_DPKG_QUERY = """#!/bin/sh
pkg=""
for arg in "$@"; do
    case "$arg" in
        -*) ;;
        *) pkg=$arg ;;
    esac
done
for installed in $UC_TEST_INSTALLED; do
    if [ "$pkg" = "$installed" ]; then
        echo "install ok installed"
        exit 0
    fi
done
if [ -n "$UC_TEST_MARK_INSTALLED" ] && [ -f "$UC_TEST_MARK_INSTALLED" ]; then
    if grep -qx "$pkg" "$UC_TEST_MARK_INSTALLED"; then
        echo "install ok installed"
        exit 0
    fi
fi
echo "unknown"
exit 1
"""

_FAKE_APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$UC_TEST_LOG"
if echo " $* " | grep -q " install "; then
    for arg in "$@"; do
        case "$arg" in
            -*|install) ;;
            *)
                if [ -n "$UC_TEST_MARK_INSTALLED" ]; then
                    echo "$arg" >> "$UC_TEST_MARK_INSTALLED"
                fi
                ;;
        esac
    done
fi
exit ${UC_TEST_APT_EXIT:-0}
"""

# Stands in for /lib/arm-linux-gnueabihf/ld-linux-armhf.so.3.
#
# UC_TEST_COMPAT=1 (default) models a CONFIG_COMPAT kernel: the loader runs and
# returns UC_TEST_LOADER_RC, whose value must not change the qemu decision.
# UC_TEST_COMPAT=0 models the sunxi64 kernel: exec is refused (126) until the
# qemu-arm binfmt handler is registered. Installing qemu-user-static is not
# enough: Debian's postinst skips qemu-arm on arm64 (it treats armhf as native),
# so only qemu-armeb appears -- which cannot run little-endian Centaur.
# UC_TEST_BINFMT_BROKEN keeps it refused even after registration.
_FAKE_ARMHF_LOADER = """#!/bin/sh
echo "armhf-loader $*" >> "$UC_TEST_LOG"
if [ "${UC_TEST_COMPAT:-1}" = "1" ]; then
    exit "${UC_TEST_LOADER_RC:-0}"
fi
if [ "${UC_TEST_BINFMT_BROKEN:-0}" != "1" ] \\
   && [ -n "${UC_BINFMT_MISC_DIR:-}" ] \\
   && [ -e "${UC_BINFMT_MISC_DIR}/qemu-arm" ]; then
    exit 0
fi
exit 126
"""

_FAKE_UPDATE_BINFMTS = """#!/bin/sh
# Log only the subcommand and name: --magic/--mask are raw ELF bytes and would
# make the test log unreadable as UTF-8.
echo "update-binfmts $1 $2" >> "$UC_TEST_LOG"
name=""
if [ "$1" = "--import" ]; then
    name=$2
elif [ "$1" = "--install" ]; then
    name=$2
fi
if [ -n "$name" ] && [ "${UC_TEST_BINFMT_EXIT:-0}" = "0" ]; then
    mkdir -p "${UC_BINFMT_MISC_DIR}"
    touch "${UC_BINFMT_MISC_DIR}/$name"
fi
exit ${UC_TEST_BINFMT_EXIT:-0}
"""


def _write_tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def helper_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "dpkg", _FAKE_DPKG)
    _write_tool(bindir, "dpkg-query", _FAKE_DPKG_QUERY)
    _write_tool(bindir, "apt-get", _FAKE_APT_GET)
    _write_tool(bindir, "update-binfmts", _FAKE_UPDATE_BINFMTS)
    loader = tmp_path / "ld-linux-armhf.so.3"
    loader.write_text(_FAKE_ARMHF_LOADER)
    loader.chmod(0o755)
    qemu_arm = tmp_path / "qemu-arm-static"
    qemu_arm.write_text("#!/bin/sh\nexit 0\n")
    qemu_arm.chmod(0o755)
    binfmt_dir = tmp_path / "binfmt_misc"
    binfmt_dir.mkdir()
    log = tmp_path / "calls.log"
    marked = tmp_path / "marked-installed"
    marked.write_text("")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["UC_TEST_LOG"] = str(log)
    env["UC_TEST_ARCH"] = "arm64"
    env["UC_TEST_INSTALLED"] = ""
    env["UC_TEST_FOREIGN_ARCH"] = ""
    env["UC_TEST_MARK_INSTALLED"] = str(marked)
    env["UC_ARMHF_LOADER"] = str(loader)
    env["UC_QEMU_ARM_STATIC"] = str(qemu_arm)
    env["UC_BINFMT_MISC_DIR"] = str(binfmt_dir)
    return env, log, tmp_path


def _update_binfmts_calls(calls):
    """update-binfmts lines from the helper's command log."""
    return [c for c in calls if c.startswith("update-binfmts ")]


def _run(env):
    proc = subprocess.run(  # noqa: S603 - pinned helper, no extra argv
        ["/bin/sh", str(_HELPER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    log = Path(env["UC_TEST_LOG"])
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _apt_install_packages(calls):
    """Package operands of ``apt-get install`` lines, in order."""
    packages = []
    for line in calls:
        parts = line.split()
        if not parts or parts[0] != "apt-get" or "install" not in parts:
            continue
        packages.extend(p for p in parts[1:] if not p.startswith("-") and p != "install")
    return packages


def test_helper_exists():
    """The import sudoers grant points at this path.

    Failure: the grant authorizes a missing file and Centaur import cannot
    provision armhf.
    """
    assert _HELPER.is_file()


def test_native_armhf_host_is_a_no_op(helper_env):
    """A 32-bit board already runs Centaur; the helper must not apt-get.

    Failure: apt-get install runs on Pi OS 32-bit and tries to add armhf as a
    foreign architecture.
    """
    env, log, _ = helper_env
    env["UC_TEST_ARCH"] = "armhf"
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("apt-get ") for c in calls)


def test_already_provisioned_compat_host_does_not_install_qemu(helper_env):
    """Pi OS 64-bit with the three armhf packages present is done.

    Failure: qemu-user-static is installed on every arm64 board, including
    ones that already run armhf ELF natively, or qemu-arm is registered and
    intercepts native AArch32.
    """
    env, _, _ = helper_env
    env["UC_TEST_INSTALLED"] = " ".join(ALL_ARMHF)
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("apt-get install") for c in calls)
    assert QEMU_PKG not in "\n".join(calls)
    assert not _update_binfmts_calls(calls)


def test_compat_host_installs_armhf_runtime_without_qemu(helper_env):
    """Missing libc6:armhf on a COMPAT host must not pull qemu-user-static.

    Failure: Orange Pi's qemu package is installed on a Pi 4 64-bit image
    that does not need it.
    """
    env, _, _ = helper_env
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    installed = _apt_install_packages(calls)
    assert ARMHF_RUNTIME_PKG in installed
    for pkg in CROSS_PACKAGES:
        assert pkg in installed
    assert QEMU_PKG not in installed
    assert not _update_binfmts_calls(calls)


def test_no_kernel_config_on_the_host_does_not_pull_qemu_onto_a_pi(helper_env):
    """The decision must not depend on a kernel config file existing.

    Why this test exists: the first version of this helper read
    ``/proc/config.gz`` and treated an absent file as "no CONFIG_COMPAT".
    Raspberry Pi OS does not enable CONFIG_IKCONFIG_PROC, so that file is not
    there and every 64-bit Pi running a Centaur import installed
    qemu-user-static plus its binfmt handlers for nothing.

    The fixture sets no kernel-config variable at all, and the loader runs, so
    a correct helper asks the kernel and installs no qemu.

    Failure: config-file archaeology comes back and the Pi regression with it.
    """
    env, _, _ = helper_env
    assert "UC_KERNEL_CONFIG" not in env
    env["UC_TEST_INSTALLED"] = " ".join(ALL_ARMHF)
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert QEMU_PKG not in "\n".join(calls)
    assert not _update_binfmts_calls(calls)


@pytest.mark.parametrize("loader_rc", ["0", "1", "127"])
def test_a_loader_that_runs_needs_no_qemu_whatever_its_exit_code(helper_env, loader_rc):
    """Only ENOEXEC (126) means "cannot exec"; other statuses mean it ran.

    Why this test exists: ``ld.so --verify`` returns non-zero for perfectly
    ordinary reasons, and the loader's own status says nothing about whether
    the kernel can execute AArch32. Treating any non-zero as failure would
    install qemu on a healthy Pi.

    Failure: the probe checks for success instead of for 126, and qemu is
    installed whenever the loader reports anything but 0.
    """
    env, _, _ = helper_env
    env["UC_TEST_INSTALLED"] = " ".join(ALL_ARMHF)
    env["UC_TEST_LOADER_RC"] = loader_rc
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert QEMU_PKG not in "\n".join(calls)
    assert not _update_binfmts_calls(calls)


def test_sunxi64_without_compat_installs_qemu_user_static(helper_env):
    """A host that refuses AArch32 (ENOEXEC) needs binfmt qemu.

    Measured on the Orange Pi Zero 2W: 6.18.45-current-sunxi64 has
    CONFIG_COMPAT unset, so an armhf ELF fails with Exec format error even
    after libc6:armhf is present. Debian's qemu-user-static postinst still
    skips qemu-arm on arm64, so the helper must register that handler itself.

    Failure: apt-get install lists only libc6:armhf / the cross toolchain,
    the qemu package is installed but qemu-arm is never registered, and
    Centaur import reports success on a board that cannot exec centaur.
    """
    env, _, tmp_path = helper_env
    env["UC_TEST_COMPAT"] = "0"
    env["UC_TEST_INSTALLED"] = " ".join(ALL_ARMHF)
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert _apt_install_packages(calls) == [QEMU_PKG]
    binfmt_calls = _update_binfmts_calls(calls)
    assert binfmt_calls, calls
    assert any("qemu-arm" in c for c in binfmt_calls)
    assert (tmp_path / "binfmt_misc" / "qemu-arm").exists()


def test_sunxi64_missing_everything_installs_armhf_then_qemu(helper_env):
    """First import on the Orange Pi installs the runtime, then qemu.

    The order matters: the exec probe is only meaningful once libc6:armhf has
    put a loader on disk, so the runtime install must precede it.

    Failure: only the Pi package set is installed and the binary still cannot
    exec, or qemu is requested before there is anything to probe.
    """
    env, _, tmp_path = helper_env
    env["UC_TEST_COMPAT"] = "0"
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    installed = _apt_install_packages(calls)
    assert ARMHF_RUNTIME_PKG in installed
    for pkg in CROSS_PACKAGES:
        assert pkg in installed
    assert QEMU_PKG in installed
    assert installed.index(ARMHF_RUNTIME_PKG) < installed.index(QEMU_PKG)
    assert any("qemu-arm" in c for c in _update_binfmts_calls(calls))
    assert (tmp_path / "binfmt_misc" / "qemu-arm").exists()


def test_qemu_package_without_qemu_arm_registers_the_handler(helper_env):
    """qemu-user-static on disk is not the same as a working qemu-arm handler.

    Why this test exists: Debian skips importing qemu-arm on arm64 because
    dpkg treats armhf as a native compatible architecture. That is correct
    on Raspberry Pi OS 64-bit (CONFIG_COMPAT) and wrong on sunxi64 without
    COMPAT, where only qemu-armeb (big-endian) is registered. The measured
    board had the package installed, qemu-armeb in binfmt_misc, and
    ``./centaur`` still died with Exec format error. The previous helper
    treated "package installed + still ENOEXEC" as a terminal failure and
    never called update-binfmts.

    Failure: the helper apt-gets qemu again, or exits 1 without registering
    qemu-arm, or registers qemu-armeb.
    """
    env, _, tmp_path = helper_env
    env["UC_TEST_COMPAT"] = "0"
    env["UC_TEST_INSTALLED"] = " ".join((*ALL_ARMHF, QEMU_PKG))
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert QEMU_PKG not in _apt_install_packages(calls)
    assert any(
        "qemu-arm" in c and "qemu-armeb" not in c for c in _update_binfmts_calls(calls)
    )
    assert (tmp_path / "binfmt_misc" / "qemu-arm").exists()


def test_compat_host_with_qemu_already_installed_does_not_register_qemu_arm(helper_env):
    """A Pi that already has qemu-user-static must keep native AArch32.

    Why this test exists: qemu-user-static may be present for unrelated
    reasons. Registering qemu-arm on a CONFIG_COMPAT kernel intercepts
    every armhf binary, including libc6:armhf, and replaces native 32-bit
    execution with qemu. The helper must not touch binfmt when the kernel
    already runs AArch32.

    Failure: update-binfmts --install/--import qemu-arm runs on Pi OS 64-bit.
    """
    env, _, _ = helper_env
    env["UC_TEST_INSTALLED"] = " ".join((*ALL_ARMHF, QEMU_PKG))
    proc, calls = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("apt-get install") for c in calls)
    assert not _update_binfmts_calls(calls)


def test_qemu_already_installed_but_binfmt_dead_fails_loudly(helper_env):
    """qemu present and AArch32 still refused must not report success.

    Why this test exists: ``ensure_armhf_support`` is a required step, not
    best-effort. Exiting 0 here would hand back a board whose centaur binary
    dies with Exec format error at launch, long after the import said it
    worked. The helper must try to register qemu-arm (Debian will not have)
    and still fail if that does not make AArch32 executable.

    Failure: the helper exits 0 with no working AArch32, or loops installing a
    package that is already present, or never attempts update-binfmts.
    """
    env, _, _ = helper_env
    env["UC_TEST_COMPAT"] = "0"
    env["UC_TEST_BINFMT_BROKEN"] = "1"
    env["UC_TEST_INSTALLED"] = " ".join((*ALL_ARMHF, QEMU_PKG))
    proc, calls = _run(env)
    assert proc.returncode == 1
    assert QEMU_PKG not in _apt_install_packages(calls)
    assert _update_binfmts_calls(calls)
    assert "AArch32 still will not execute" in proc.stdout


def test_absent_loader_after_install_fails_rather_than_claiming_success(helper_env):
    """No AArch32 loader on disk is a failed provision, not a silent pass.

    Why this test exists: the probe needs a binary to run. If libc6:armhf
    reported installed but put no loader where this helper looks, the honest
    answer is "cannot verify", and the caller treats that as a failed import
    rather than launching centaur and crashing.

    Failure: a missing loader is read as "fine" and the import reports success.
    """
    env, _, tmp_path = helper_env
    env["UC_ARMHF_LOADER"] = str(tmp_path / "no-such-loader")
    env["UC_TEST_INSTALLED"] = " ".join((*ALL_ARMHF, QEMU_PKG))
    proc, _ = _run(env)
    assert proc.returncode == 1


def test_closed_package_set_does_not_take_arguments(helper_env):
    """The sudoers grant must not become apt-get install of caller packages.

    Failure: extra argv is forwarded to apt-get.
    """
    env, _, _ = helper_env
    proc = subprocess.run(  # noqa: S603
        ["/bin/sh", str(_HELPER), "netcat-openbsd"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    log_path = Path(env["UC_TEST_LOG"])
    calls = log_path.read_text().splitlines() if log_path.exists() else []
    assert "netcat-openbsd" not in "\n".join(calls)
    # Extra argv is ignored (helper takes none); it still exits 0 on success.
    assert proc.returncode == 0

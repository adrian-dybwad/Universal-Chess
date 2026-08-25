"""Tests for the pinned centaur-import-mount root helper.

The postinst grants the service user passwordless sudo on exactly this script,
so it is the security boundary for that grant. These tests run the *real* script
with a fake mount/umount/mountpoint on PATH to pin:

1. That a mount goes through read-only and with loop/nodev/nosuid/noexec -- the
   options that keep a hostile/corrupt SD image from being written back or from
   carrying device nodes or setuid/exec payloads into the system.
2. That the helper refuses any image outside the allowed dir, any traversal
   token, any unknown subcommand, and any extra arguments -- the boundary the
   NOPASSWD grant relies on. A regression that widened any of these would turn
   the grant into a broad "mount anything anywhere" root primitive.

The fixed paths are overridden via env here only because sudo's env_reset strips
them under the real grant (so production always uses the hardcoded defaults).
"""

import os
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "centaur-import-mount"

_FAKE_TOOL = """#!/bin/sh
echo "{name} $*" >> "$CENTAUR_MOUNT_TEST_LOG"
exit 0
"""
# mountpoint must report "is a mountpoint" so the umount branch proceeds.
_FAKE_MOUNTPOINT = """#!/bin/sh
echo "mountpoint $*" >> "$CENTAUR_MOUNT_TEST_LOG"
exit 0
"""


@pytest.fixture
def env_and_log(tmp_path):
    """Fake mount/umount/mountpoint on PATH plus scratch mnt/img dirs and a log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("mount", "umount"):
        p = bindir / tool
        p.write_text(_FAKE_TOOL.format(name=tool))
        p.chmod(0o755)
    mp = bindir / "mountpoint"
    mp.write_text(_FAKE_MOUNTPOINT)
    mp.chmod(0o755)

    img_dir = tmp_path / "imgdir"
    img_dir.mkdir()
    mnt = tmp_path / "mnt"

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["CENTAUR_MOUNT_TEST_LOG"] = str(log)
    env["CENTAUR_IMPORT_MNT"] = str(mnt)
    env["CENTAUR_IMPORT_IMG_DIR"] = str(img_dir)
    return env, log, img_dir, mnt


def _run(env, *args):
    proc = subprocess.run(  # noqa: S603 - test invokes the pinned helper with fixed args
        ["/bin/sh", str(_HELPER), *args],
        env=env, capture_output=True, text=True,
    )
    log = Path(env["CENTAUR_MOUNT_TEST_LOG"])
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_mount_uses_readonly_loop_with_hardening_options(env_and_log):
    # The core safety contract: an allowed image is mounted ro + loop and with
    # nodev/nosuid/noexec. Losing 'ro' would let a corrupt image be written back;
    # losing noexec/nosuid would let the mounted tree carry exec/setuid payloads.
    # 'noload' is part of the contract too: an SD image captured from a Centaur
    # that was not cleanly unmounted has a dirty ext4 journal, and without noload
    # the kernel refuses the mount outright ("write access unavailable, cannot
    # proceed") because journal replay cannot write to a read-only loop device.
    # Dropping it makes every unclean image fail with mount exit 32.
    env, _log, img_dir, mnt = env_and_log
    img = img_dir / "centaur-sd.img"
    img.write_bytes(b"\x00")
    proc, calls = _run(env, "mount", str(img))
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"mount -o ro,noload,loop,nodev,nosuid,noexec {img} {mnt}"]


def test_umount_releases_the_fixed_mountpoint(env_and_log):
    # umount must target only the fixed mountpoint (lazy, to release the loop dev
    # even if a copy still holds a handle).
    env, _log, _img_dir, mnt = env_and_log
    proc, calls = _run(env, "umount")
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"mountpoint -q {mnt}", f"umount -l {mnt}"]


def test_image_outside_allowed_dir_is_refused(env_and_log):
    # Boundary: an image path outside the allowed dir must be rejected before any
    # mount runs, so the grant cannot loop-mount an arbitrary file.
    env, _log, _img_dir, _mnt = env_and_log
    proc, calls = _run(env, "mount", "/etc/shadow")
    assert proc.returncode == 3
    assert calls == []


def test_traversal_in_image_path_is_refused(env_and_log):
    # Boundary: a traversal token must be rejected even when the prefix matches,
    # so '<allowed>/../../etc/x' cannot escape the allowed dir.
    env, _log, img_dir, _mnt = env_and_log
    proc, calls = _run(env, "mount", f"{img_dir}/../../etc/passwd")
    assert proc.returncode == 3
    assert calls == []


def test_unknown_subcommand_is_refused(env_and_log):
    # Boundary: anything other than mount/umount must exit 2 and run nothing.
    env, _log, _img_dir, _mnt = env_and_log
    proc, calls = _run(env, "format-disk")
    assert proc.returncode == 2
    assert calls == []


def test_mount_with_extra_arguments_is_refused(env_and_log):
    # Boundary: exactly one image arg is allowed; extra args (smuggling options
    # into mount) are rejected before anything runs.
    env, _log, img_dir, _mnt = env_and_log
    img = img_dir / "centaur-sd.img"
    img.write_bytes(b"\x00")
    proc, calls = _run(env, "mount", str(img), "-o", "rw")
    assert proc.returncode == 2
    assert calls == []


def test_stage_copies_app_subtree_and_makes_it_readable(env_and_log):
    # Why: the SD app's engines/fonts/books dirs are often mode-restricted or
    # owned by a different uid, so the unprivileged service user cannot read into
    # them on the read-only mount and a plain copy fails with EPERM. The privileged
    # 'stage' verb copies the detected app dir (under the mount) into the service
    # tmp dir as root and makes it world-readable, so the subsequent unprivileged
    # copytree can read every entry. This runs the real cp/chmod (only mount/umount
    # are faked), so it proves the staged tree is actually readable.
    env, _log, _img_dir, mnt = env_and_log
    # A restricted source dir mimicking the SD's 0700 engines/ that broke the copy.
    src = mnt / "home" / "pi" / "centaur"
    (src / "engines").mkdir(parents=True)
    (src / "engines" / "stockfish_pi").write_text("#!engine\n")
    (src / "centaur").write_text("#!fake\n")
    # The rule reads 0o700 as over-permissive, which is backwards here: this is
    # the restrictive mode the SD card actually carries, and reproducing it is
    # the whole point of the test. Relaxing it would remove the EPERM that
    # 'stage' exists to work around, leaving the test passing on any helper.
    os.chmod(src / "engines", 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions

    staging = Path(env["CENTAUR_IMPORT_IMG_DIR"]) / "centaur-stage"
    proc, _calls = _run(env, "stage", str(src), str(staging))
    assert proc.returncode == 0, proc.stderr
    # Whole subtree staged, including the restricted dir's contents.
    assert (staging / "centaur").is_file()
    assert (staging / "engines" / "stockfish_pi").is_file()
    # World-readable/searchable so the unprivileged copy can recurse in.
    assert os.stat(staging / "engines").st_mode & 0o005 == 0o005


def test_stage_refuses_source_outside_mount(env_and_log):
    # Boundary: stage may only read from inside the fixed mountpoint, so it cannot
    # be turned into a root "copy any path" primitive.
    env, _log, _img_dir, _mnt = env_and_log
    staging = Path(env["CENTAUR_IMPORT_IMG_DIR"]) / "centaur-stage"
    proc, _calls = _run(env, "stage", "/etc", str(staging))
    assert proc.returncode == 3
    assert not staging.exists()


def test_stage_refuses_destination_outside_tmp(env_and_log):
    # Boundary: stage may only write into the allowed tmp dir, so it cannot be used
    # to overwrite an arbitrary directory as root.
    env, _log, _img_dir, mnt = env_and_log
    src = mnt / "home" / "pi" / "centaur"
    src.mkdir(parents=True)
    (src / "centaur").write_text("#!fake\n")
    proc, _calls = _run(env, "stage", str(src), "/etc/centaur-stage")
    assert proc.returncode == 3

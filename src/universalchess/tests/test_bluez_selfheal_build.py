"""Tests for the bluez-selfheal on-device bluetoothd rebuild (scripts/bluez-selfheal).

The self-heal builds a patched bluetoothd from the distro's own bluez source when
the stock daemon fails to advertise on the running kernel. On a RAM-constrained
board (a ~415MB Pi Zero 2 W) that build previously ran ``make -j$(nproc)`` with
the distro's default ``-flto=auto``; the parallel LTO link thrashed swap for tens
of minutes (load ~13, kswapd pegged). These regressions can only surface as wall
time on-device, so the two deterministic guards are:

  1. the compile-parallelism maths (via the ``build-jobs`` diagnostic subcommand,
     exercised with RAM/core test seams), and
  2. the build recipe text (LTO disabled, RAM-aware ``-j`` wired in).

Each test states the regression it guards and how that regression would surface.
"""

import os
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "bluez-selfheal"


def _build_jobs(mem_mb=None, cores=None, override=None):
    """Run the ``build-jobs`` diagnostic and return (int jobs, proc)."""
    env = dict(os.environ)
    # Isolate from any ambient override the dev shell might carry.
    env.pop("UC_BUILD_PARALLELISM", None)
    if override is not None:
        env["UC_BUILD_PARALLELISM"] = override
    if mem_mb is not None:
        env["UC_BLUEZ_MEMTOTAL_MB"] = str(mem_mb)
    if cores is not None:
        env["UC_BUILD_NPROC"] = str(cores)
    proc = subprocess.run(  # noqa: S603 - test invokes the pinned helper with fixed args
        ["bash", str(_HELPER), "build-jobs"],  # noqa: S607 - bash on PATH is fine in tests
        env=env, capture_output=True, text=True,
    )
    jobs = int(proc.stdout.strip()) if proc.stdout.strip().isdigit() else None
    return jobs, proc


# --------------------------------------------------------------------------- #
# Compile parallelism maths
# --------------------------------------------------------------------------- #

def test_jobs_floored_to_one_on_low_ram_board():
    # A 415MB / 4-core board must compile with -j1: one memory-heavy job already
    # approaches RAM, so -j4 (=nproc) oversubscribes memory and thrashes swap for
    # tens of minutes. Manifests as >1 here if the RAM bound is dropped and the
    # job count reverts to the core count.
    jobs, proc = _build_jobs(mem_mb=415, cores=4)
    assert proc.returncode == 0, proc.stderr
    assert jobs == 1


def test_jobs_use_core_count_when_ram_ample():
    # An 8GB / 4-core board keeps full core parallelism -- the RAM bound must not
    # throttle capable hardware. Manifests as <4 if the min() clamps too hard.
    jobs, proc = _build_jobs(mem_mb=8192, cores=4)
    assert proc.returncode == 0, proc.stderr
    assert jobs == 4


def test_jobs_bounded_by_ram_between_extremes():
    # A 2GB / 4-core board is RAM-bound to 2 jobs (2048 / 1024MB-per-job), below
    # its 4 cores. Guards the divisor: a regression that ignored RAM would give 4,
    # one that mis-scaled the divisor would give a different count.
    jobs, proc = _build_jobs(mem_mb=2048, cores=4)
    assert proc.returncode == 0, proc.stderr
    assert jobs == 2


def test_explicit_override_wins_over_ram_and_cores():
    # The UC_BUILD_PARALLELISM knob (shared with the engine builder) must win so a
    # board can be tuned without a code change. Manifests as the RAM/core-derived
    # value if the override branch is skipped.
    jobs, proc = _build_jobs(mem_mb=415, cores=4, override="3")
    assert proc.returncode == 0, proc.stderr
    assert jobs == 3


def test_invalid_override_falls_back_to_ram_bound():
    # A garbage/zero override must be ignored (not passed through as -j0/-jfoo,
    # which would run make unbounded or abort), falling back to the RAM bound.
    for bad in ("0", "notanint", "-2"):
        jobs, proc = _build_jobs(mem_mb=415, cores=4, override=bad)
        assert proc.returncode == 0, f"{bad!r}: {proc.stderr}"
        assert jobs == 1, f"override {bad!r} should be ignored -> RAM bound (1)"


def test_jobs_fall_back_to_cores_when_ram_unreadable():
    # If RAM cannot be read (non-Linux, or a missing /proc), the count must fall
    # back to the core count rather than crash or return 0. Simulated by an empty
    # RAM seam. Manifests as a non-numeric/0 output if the fallback is missing.
    jobs, proc = _build_jobs(mem_mb="", cores=4)
    assert proc.returncode == 0, proc.stderr
    assert jobs == 4


# --------------------------------------------------------------------------- #
# Build recipe text (only reproduces at compile time on-device)
# --------------------------------------------------------------------------- #

def test_build_disables_lto():
    # The bluetoothd rebuild must strip LTO. The patch is a one-line functional
    # fix, but the distro's default -flto=auto makes the final link the memory +
    # time peak (parallel lto1-ltrans thrashing swap). Removing it via
    # dpkg-buildflags (optimize=-lto) makes -j the only concurrency knob.
    # Manifests as the multi-tens-of-minutes thrash returning if this is dropped.
    content = _HELPER.read_text()
    assert "optimize=-lto" in content
    assert "DEB_BUILD_MAINT_OPTIONS" in content


def test_build_uses_ram_aware_jobs_not_bare_nproc():
    # The daemon build must use the RAM-aware build_jobs value, never the old bare
    # `make -j$(nproc) src/bluetoothd`. Manifests as unbounded core-count
    # parallelism (the swap thrash) if the recipe reverts.
    content = _HELPER.read_text()
    assert 'make -j"${jobs}" src/bluetoothd' in content
    assert 'make -j"$(nproc)" src/bluetoothd' not in content
    # jobs must come from the bounded helper, not a raw nproc.
    assert 'jobs="$(build_jobs)"' in content

"""Tests for the uc-runtime-swap root helper (scripts/uc-runtime-swap).

Unlike uc-build-memory (which brings swap up only around a heavy build and tears
it down after), this helper provisions a PERSISTENT disk-backed swapfile so a
RAM-constrained board has real headroom during normal play -- e.g. three or four
engines loaded at once for player 1, player 2 and analysis -- instead of OOM-
killing the engine subprocesses. It is idempotent: run at every boot by a systemd
oneshot, it creates/sizes the swapfile only when needed and otherwise just
re-activates the file that already persists on disk, so it does not rewrite the
SD card on every boot.

The script is exercised in DRY_RUN mode: every privileged action is recorded to a
log instead of executed, and RAM / existing-swap / file-state are injected via
environment seams. This pins the sizing maths and the create-vs-reactivate-vs-
resize decision (the parts that decide *whether* and *what* to write) without root
or a real board.

Each test states the regression it guards and how that regression would surface.
"""

import os
import subprocess
from pathlib import Path

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-runtime-swap"


def _env(**overrides):
    env = dict(os.environ)
    env["UC_RUNTIME_SWAP_DRY_RUN"] = "1"
    # Sensible low-RAM board defaults; individual tests override as needed.
    env.setdefault("UC_RUNTIME_SWAP_MEMTOTAL_MB", "415")
    env.setdefault("UC_RUNTIME_SWAP_SWAPTOTAL_MB", "415")  # stock zram only
    env.setdefault("UC_RUNTIME_SWAP_EXISTING_MB", "0")     # no swapfile yet
    env.setdefault("UC_RUNTIME_SWAP_ACTIVE", "0")
    env.setdefault("UC_RUNTIME_SWAP_TARGET_MB", "4096")
    env.setdefault("UC_RUNTIME_SWAP_PRIORITY", "10")
    env.setdefault("UC_RUNTIME_SWAP_KEEP_FREE_MB", "512")
    env.setdefault("UC_RUNTIME_SWAP_TOLERANCE_MB", "128")
    env.setdefault("UC_RUNTIME_SWAP_FS_FREE_MB", "49000")  # plenty of card free
    env.setdefault("UC_RUNTIME_SWAP_FILE", "/var/cache/universalchess/runtime-swap")
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _run(action, action_log=None, **overrides):
    env = _env(**overrides)
    if action_log is not None:
        env["UC_RUNTIME_SWAP_ACTION_LOG"] = str(action_log)
    # The helper path is resolved from __file__ and the argv is a fixed command
    # ("bash <helper> <action>"), not untrusted input -- safe to run directly.
    proc = subprocess.run(  # noqa: S603
        ["bash", str(_HELPER), action],  # noqa: S607
        env=env, capture_output=True, text=True,
    )
    lines = (
        Path(action_log).read_text().splitlines()
        if action_log is not None and Path(action_log).exists()
        else []
    )
    return proc, lines


def _status_fields(**overrides):
    proc, _ = _run("status", **overrides)
    assert proc.returncode == 0, proc.stderr
    fields = {}
    for tok in proc.stdout.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields, proc


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #

def test_desired_size_fills_gap_to_target_on_low_ram_board():
    # On a 415MB board whose only swap is the ~415MB stock zram, reaching a 4096MB
    # total budget needs a disk swapfile of 4096-415-415=3266MB. A regression in
    # the budget maths (double-counting RAM, ignoring stock swap) changes
    # desired_file_mb and the board would get too little or too much swap.
    f, _ = _status_fields(UC_RUNTIME_SWAP_MEMTOTAL_MB="415",
                          UC_RUNTIME_SWAP_SWAPTOTAL_MB="415",
                          UC_RUNTIME_SWAP_TARGET_MB="4096")
    assert f["desired_file_mb"] == "3266"


def test_desired_excludes_our_own_already_active_swapfile():
    # Second boot: our 3266MB swapfile is already active, so /proc SwapTotal is
    # 415(stock)+3266(ours)=3681. desired must still be 3266 -- computed against
    # OTHER swap only (3681-3266=415). If the helper counted its own file as
    # "existing swap", desired would collapse to 0 and it would delete the
    # swapfile it just re-activated, thrashing the card every boot.
    f, _ = _status_fields(UC_RUNTIME_SWAP_MEMTOTAL_MB="415",
                          UC_RUNTIME_SWAP_SWAPTOTAL_MB="3681",
                          UC_RUNTIME_SWAP_EXISTING_MB="3266",
                          UC_RUNTIME_SWAP_ACTIVE="1",
                          UC_RUNTIME_SWAP_TARGET_MB="4096")
    assert f["desired_file_mb"] == "3266"


def test_desired_zero_on_high_ram_board():
    # A 4GB board already meets a 4GB target, so no persistent swapfile should be
    # provisioned (desired 0). A regression that always sizes a file would wear
    # the card on capable boards; this pins desired=0 there.
    f, _ = _status_fields(UC_RUNTIME_SWAP_MEMTOTAL_MB="4096",
                          UC_RUNTIME_SWAP_SWAPTOTAL_MB="0",
                          UC_RUNTIME_SWAP_TARGET_MB="4096")
    assert f["desired_file_mb"] == "0"


def test_desired_is_capped_by_free_card_space():
    # The swapfile must never fill the SD card. With only 1000MB free and a 512MB
    # keep-free floor, the file is capped at 1000-512=488MB even though the budget
    # wants 3266MB. A regression dropping the cap would exhaust the card and can
    # brick writes; this pins the cap.
    f, _ = _status_fields(UC_RUNTIME_SWAP_MEMTOTAL_MB="415",
                          UC_RUNTIME_SWAP_SWAPTOTAL_MB="415",
                          UC_RUNTIME_SWAP_TARGET_MB="4096",
                          UC_RUNTIME_SWAP_FS_FREE_MB="1000",
                          UC_RUNTIME_SWAP_KEEP_FREE_MB="512")
    assert f["desired_file_mb"] == "488"


# --------------------------------------------------------------------------- #
# ensure: create / reactivate / resize / remove decision
# --------------------------------------------------------------------------- #

def test_ensure_creates_and_activates_when_absent(tmp_path):
    # First provision on a fresh low-RAM board: the file does not exist, so ensure
    # must allocate it, mark it swap, and swap it on at the low backstop priority.
    # If any step is dropped the board gets no runtime swap and still OOMs; the
    # create-before-activate order is asserted so the file exists before swapon.
    log = tmp_path / "actions.log"
    proc, lines = _run("ensure", action_log=log,
                       UC_RUNTIME_SWAP_EXISTING_MB="0",
                       UC_RUNTIME_SWAP_ACTIVE="0")
    assert proc.returncode == 0, proc.stderr
    joined = "\n".join(lines)
    assert any(ln.startswith("fallocate -l 3266M ") for ln in lines), joined
    assert any(ln.startswith("chmod 600 ") for ln in lines), joined
    assert any(ln.startswith("mkswap ") for ln in lines), joined
    assert any(ln.startswith("swapon -p 10 ") for ln in lines), joined
    create_i = next(i for i, ln in enumerate(lines) if ln.startswith("mkswap "))
    on_i = next(i for i, ln in enumerate(lines) if ln.startswith("swapon -p 10 "))
    assert create_i < on_i


def test_ensure_reactivates_persisted_file_without_rewriting(tmp_path):
    # After a reboot the swapfile still exists on disk at the right size but is
    # inactive. ensure must simply swap it back on -- NOT re-allocate or re-mkswap
    # it. A regression that recreates every boot would rewrite gigabytes to the SD
    # card on each start (the wear this test guards against).
    log = tmp_path / "actions.log"
    proc, lines = _run("ensure", action_log=log,
                       UC_RUNTIME_SWAP_EXISTING_MB="3266",
                       UC_RUNTIME_SWAP_ACTIVE="0")
    assert proc.returncode == 0, proc.stderr
    assert any(ln.startswith("swapon -p 10 ") for ln in lines)
    assert not any(ln.startswith("fallocate") for ln in lines)
    assert not any(ln.startswith("mkswap") for ln in lines)
    assert not any(ln.startswith("rm -f") for ln in lines)


def test_ensure_is_noop_when_already_active_and_correctly_sized(tmp_path):
    # Steady state: the correct swapfile is already active. ensure must do nothing
    # -- no swapon, no rewrite. A regression that re-runs swapon or recreates here
    # would churn the card / swap subsystem on every idempotent invocation.
    # Active file => /proc SwapTotal includes it: 415 stock zram + 3266 ours.
    log = tmp_path / "actions.log"
    proc, lines = _run("ensure", action_log=log,
                       UC_RUNTIME_SWAP_SWAPTOTAL_MB="3681",
                       UC_RUNTIME_SWAP_EXISTING_MB="3266",
                       UC_RUNTIME_SWAP_ACTIVE="1")
    assert proc.returncode == 0, proc.stderr
    assert lines == [], "\n".join(lines)


def test_ensure_resizes_when_existing_file_is_out_of_tolerance(tmp_path):
    # If the existing file is far from the target (e.g. an old 1000MB file, or the
    # board's RAM/target changed), ensure must swap it off, remove it, and rebuild
    # at the new size. A regression that keeps a wrong-sized file would leave the
    # board under-provisioned; this pins the swapoff->rm->recreate sequence.
    # Active 1000MB file => /proc SwapTotal is 415 stock zram + 1000 ours = 1415.
    log = tmp_path / "actions.log"
    proc, lines = _run("ensure", action_log=log,
                       UC_RUNTIME_SWAP_SWAPTOTAL_MB="1415",
                       UC_RUNTIME_SWAP_EXISTING_MB="1000",
                       UC_RUNTIME_SWAP_ACTIVE="1")
    assert proc.returncode == 0, proc.stderr
    assert any(ln.startswith("swapoff ") for ln in lines)
    assert any(ln.startswith("rm -f ") for ln in lines)
    assert any(ln.startswith("fallocate -l 3266M ") for ln in lines)


def test_ensure_removes_swapfile_on_high_ram_board(tmp_path):
    # If desired is 0 (a capable board) but a stale swapfile exists and is active,
    # ensure must swap it off and delete it rather than leave needless SD swap. A
    # regression that only ever adds swap would leave the file resident forever.
    log = tmp_path / "actions.log"
    proc, lines = _run("ensure", action_log=log,
                       UC_RUNTIME_SWAP_MEMTOTAL_MB="4096",
                       UC_RUNTIME_SWAP_SWAPTOTAL_MB="0",
                       UC_RUNTIME_SWAP_TARGET_MB="4096",
                       UC_RUNTIME_SWAP_EXISTING_MB="2000",
                       UC_RUNTIME_SWAP_ACTIVE="1")
    assert proc.returncode == 0, proc.stderr
    assert any(ln.startswith("swapoff ") for ln in lines)
    assert any(ln.startswith("rm -f ") for ln in lines)
    assert not any(ln.startswith("fallocate") for ln in lines)


# --------------------------------------------------------------------------- #
# remove command and argument boundary
# --------------------------------------------------------------------------- #

def test_remove_swaps_off_and_deletes(tmp_path):
    # The explicit teardown path (e.g. package removal) must swap off and delete
    # the file so no orphan swap lingers. Guards that remove is not a no-op.
    log = tmp_path / "actions.log"
    proc, lines = _run("remove", action_log=log,
                       UC_RUNTIME_SWAP_EXISTING_MB="3266",
                       UC_RUNTIME_SWAP_ACTIVE="1")
    assert proc.returncode == 0, proc.stderr
    assert any(ln.startswith("swapoff ") for ln in lines)
    assert any(ln.startswith("rm -f ") for ln in lines)


def test_unknown_action_is_refused(tmp_path):
    # Only ensure/status/remove are valid actions; anything else must exit non-zero
    # and perform no privileged action. Guards the command surface from typos in
    # the systemd unit silently doing nothing (or something unintended).
    log = tmp_path / "actions.log"
    proc, lines = _run("frobnicate", action_log=log)
    assert proc.returncode == 2
    assert lines == []

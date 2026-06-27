"""Tests for the uc-build-memory root helper (scripts/uc-build-memory).

This helper temporarily expands swap (zram + an SD-card backstop + a swappiness
bump) for the duration of a heavy operation (engine source build or BlueZ
self-heal rebuild) and tears it down afterwards, so OOM-prone builds complete on
RAM-constrained boards without leaving permanent swap that wears the SD card or
slows normal play. It is a pinned passwordless-sudo helper (security boundary),
mirroring bt-admin.

The real script is run in DRY_RUN mode, which records every privileged action to
a log instead of executing it, and with RAM/swap/state-dir overrides. This lets
the sizing maths and the reference-counting transitions (the parts that decide
*whether* and *what* to set up/tear down) be pinned without root or a real board.

Each test states the regression it guards and how that regression would surface.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-build-memory"

# A PID that cannot exist (above every platform's max PID) so liveness always
# reports it dead -- used to exercise stale-token pruning deterministically.
_DEAD_PID = "999999"
# init: always alive, used where a single holder acquires and releases under the
# same PID (the token is removed explicitly before any prune, so its liveness is
# immaterial).
_ALIVE_PID = "1"


@pytest.fixture
def live_pid():
    """A real, same-user, long-lived process PID for multi-holder tests.

    Multi-holder tests need a second holder whose token must SURVIVE pruning. A
    same-user live process makes the helper's liveness check succeed via the
    fork-free `kill` builtin path, so the test is deterministic (using another
    user's PID would hit the permission-denied branch, and a fabricated PID would
    be pruned)."""
    proc = subprocess.Popen(["sleep", "120"])
    try:
        yield str(proc.pid)
    finally:
        proc.terminate()
        proc.wait()


def _run(action, *extra, state_dir, action_log, mem_mb="415", swap_mb="414",
         target_mb="1200", swapfile=None):
    """Invoke the helper in dry-run; return (proc, action_lines, state_dir Path)."""
    env = dict(os.environ)
    env["UC_BUILD_MEM_DRY_RUN"] = "1"
    env["UC_BUILD_MEM_ACTION_LOG"] = str(action_log)
    env["UC_BUILD_MEM_STATE_DIR"] = str(state_dir)
    env["UC_BUILD_MEM_SWAPFILE"] = str(swapfile or (Path(state_dir) / "build-swap"))
    env["UC_BUILD_MEM_MEMTOTAL_MB"] = mem_mb
    env["UC_BUILD_MEM_SWAPTOTAL_MB"] = swap_mb
    env["UC_BUILD_MEM_TARGET_MB"] = target_mb
    proc = subprocess.run(
        ["bash", str(_HELPER), action, *extra],
        env=env, capture_output=True, text=True,
    )
    lines = action_log.read_text().splitlines() if action_log.exists() else []
    return proc, lines


def _status(state_dir, **kw):
    env = dict(os.environ)
    env["UC_BUILD_MEM_STATE_DIR"] = str(state_dir)
    env["UC_BUILD_MEM_SWAPFILE"] = str(Path(state_dir) / "build-swap")
    env["UC_BUILD_MEM_MEMTOTAL_MB"] = kw.get("mem_mb", "415")
    env["UC_BUILD_MEM_SWAPTOTAL_MB"] = kw.get("swap_mb", "414")
    env["UC_BUILD_MEM_TARGET_MB"] = kw.get("target_mb", "4096")
    return subprocess.run(
        ["bash", str(_HELPER), "status"],
        env=env, capture_output=True, text=True,
    )


def _parse_status(stdout):
    fields = {}
    for tok in stdout.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #

def test_zram_is_half_of_ram():
    # zram is sized to RAM/2 -- a fast first swap tier that does not itself eat so
    # much RAM that it competes with the compile. A regression that mis-sizes it
    # (e.g. 1x RAM) would change planned_zram_mb here.
    proc = _status_tmp(mem_mb="512")
    assert proc.returncode == 0, proc.stderr
    assert _parse_status(proc.stdout)["planned_zram_mb"] == "256"


def test_sd_backstop_fills_gap_to_target_on_low_ram_board(tmp_path):
    # On a 415MB board with 414MB stock swap, reaching a 1200MB budget needs an SD
    # backstop of 1200-415-414-(415/2)=164MB. A regression in the budget maths
    # (double-counting RAM, ignoring stock swap) changes planned_sd_swap_mb.
    proc = subprocess.run(
        ["bash", str(_HELPER), "status"],
        env={**os.environ,
             "UC_BUILD_MEM_STATE_DIR": str(tmp_path),
             "UC_BUILD_MEM_SWAPFILE": str(tmp_path / "build-swap"),
             "UC_BUILD_MEM_MEMTOTAL_MB": "415",
             "UC_BUILD_MEM_SWAPTOTAL_MB": "414",
             "UC_BUILD_MEM_TARGET_MB": "1200"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    f = _parse_status(proc.stdout)
    assert f["planned_zram_mb"] == "207"  # 415 // 2
    assert f["planned_sd_swap_mb"] == "164"  # 1200 - 415 - 414 - 207


def test_no_sd_backstop_when_ram_meets_target(tmp_path):
    # A high-RAM board (4GB) already meets a 4GB target, so no SD swapfile should
    # be planned (0). A regression that always creates a swapfile would wear the
    # card on capable boards; this pins sd=0 there.
    proc = subprocess.run(
        ["bash", str(_HELPER), "status"],
        env={**os.environ,
             "UC_BUILD_MEM_STATE_DIR": str(tmp_path),
             "UC_BUILD_MEM_SWAPFILE": str(tmp_path / "build-swap"),
             "UC_BUILD_MEM_MEMTOTAL_MB": "4096",
             "UC_BUILD_MEM_SWAPTOTAL_MB": "0",
             "UC_BUILD_MEM_TARGET_MB": "4096"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _parse_status(proc.stdout)["planned_sd_swap_mb"] == "0"


def _status_tmp(**kw):
    return _status(tempfile.mkdtemp(), **kw)


# --------------------------------------------------------------------------- #
# Acquire / release privileged action sequence
# --------------------------------------------------------------------------- #

def test_acquire_brings_up_zram_then_sd_then_swappiness(tmp_path):
    # The whole point of the helper: a single acquire must enable the zram tier
    # (high priority), the SD backstop (low priority), and raise swappiness. If a
    # tier is dropped the build OOMs again, so each privileged step is pinned and
    # priority order (zram -p 100 before SD -p 10) is asserted.
    log = tmp_path / "actions.log"
    proc, lines = _run("acquire", "--owner-pid", _ALIVE_PID,
                       state_dir=tmp_path / "state", action_log=log)
    assert proc.returncode == 0, proc.stderr
    joined = "\n".join(lines)
    assert "modprobe zram" in joined
    assert any(ln.startswith("zramctl --find --size 207M") for ln in lines)
    assert "swapon -p 100 /dev/zram-dryrun" in lines
    assert any(ln.startswith("swapon -p 10 ") for ln in lines)
    assert "sysctl -q -w vm.swappiness=60" in lines
    # zram (high priority) must be swapped on before the SD backstop (low prio).
    assert lines.index("swapon -p 100 /dev/zram-dryrun") < \
        next(i for i, ln in enumerate(lines) if ln.startswith("swapon -p 10 "))


def test_acquire_records_a_live_token(tmp_path):
    # The token is what keeps memory up while the holder works; without it the
    # next release would tear down swap mid-build. Pin that acquire creates it.
    state = tmp_path / "state"
    _run("acquire", "--owner-pid", _ALIVE_PID, state_dir=state,
         action_log=tmp_path / "a.log")
    assert (state / "tokens" / _ALIVE_PID).exists()


def test_release_tears_down_everything_it_created(tmp_path):
    # The teardown is what prevents permanent SD wear/slow play. After the last
    # holder releases, swappiness must be restored and both swaps removed. A
    # regression that leaks the swapfile would surface as a missing
    # swapoff/rm/reset here.
    state = tmp_path / "state"
    log = tmp_path / "actions.log"
    _run("acquire", "--owner-pid", _ALIVE_PID, state_dir=state, action_log=log)
    proc, lines = _run("release", "--owner-pid", _ALIVE_PID, state_dir=state,
                       action_log=log)
    assert proc.returncode == 0, proc.stderr
    joined = "\n".join(lines)
    assert "swapoff /dev/zram-dryrun" in lines
    assert "zramctl --reset /dev/zram-dryrun" in lines
    assert any(ln.startswith("swapoff ") and "build-swap" in ln for ln in lines)
    assert any(ln.startswith("rm -f ") and "build-swap" in ln for ln in lines)
    # swappiness restored to the captured prior value (60 in dry-run).
    assert joined.count("sysctl -q -w vm.swappiness=60") >= 1
    # State cleared so a later acquire starts fresh.
    assert not (state / "created.env").exists()
    assert not (state / "tokens" / _ALIVE_PID).exists()


# --------------------------------------------------------------------------- #
# Reference counting under overlap
# --------------------------------------------------------------------------- #

def test_second_acquire_reuses_existing_memory(tmp_path, live_pid):
    # An engine install can overlap a BlueZ self-heal (apt triggers it). The
    # second acquire must NOT set up a second zram/swapfile -- exactly one setup.
    # A regression (no ref counting) would run modprobe/swapon twice and could
    # tear down the other operation's swap.
    state = tmp_path / "state"
    log = tmp_path / "actions.log"
    _run("acquire", "--owner-pid", live_pid, state_dir=state, action_log=log)
    _run("acquire", "--owner-pid", str(os.getpid()), state_dir=state, action_log=log)
    lines = log.read_text().splitlines()
    assert lines.count("modprobe zram") == 1


def test_release_with_holders_remaining_does_not_tear_down(tmp_path, live_pid):
    # With two live holders, releasing one must keep memory up for the other --
    # tearing down swap while the second build still needs it reintroduces OOM.
    state = tmp_path / "state"
    log = tmp_path / "actions.log"
    _run("acquire", "--owner-pid", live_pid, state_dir=state, action_log=log)
    _run("acquire", "--owner-pid", str(os.getpid()), state_dir=state, action_log=log)
    log.write_text("")  # isolate the release's actions
    _run("release", "--owner-pid", live_pid, state_dir=state, action_log=log)
    lines = log.read_text().splitlines()
    assert "zramctl --reset /dev/zram-dryrun" not in lines  # no teardown yet
    # The still-live holder's token survives.
    assert (state / "tokens" / str(os.getpid())).exists()


def test_last_release_tears_down(tmp_path, live_pid):
    # Releasing the final holder must tear everything down. Guards the count
    # reaching zero -> teardown transition.
    state = tmp_path / "state"
    log = tmp_path / "actions.log"
    _run("acquire", "--owner-pid", live_pid, state_dir=state, action_log=log)
    _run("acquire", "--owner-pid", str(os.getpid()), state_dir=state, action_log=log)
    _run("release", "--owner-pid", live_pid, state_dir=state, action_log=log)
    log.write_text("")
    _run("release", "--owner-pid", str(os.getpid()), state_dir=state, action_log=log)
    lines = log.read_text().splitlines()
    assert "zramctl --reset /dev/zram-dryrun" in lines


def test_dead_holder_token_is_pruned_allowing_teardown(tmp_path):
    # A build that crashes without releasing must not pin swap forever. A token
    # whose owner PID is dead is pruned on the next acquire/release; here a
    # release by a different owner prunes the dead token and tears down. Without
    # pruning, swap would persist until reboot (the leak this guards against).
    state = tmp_path / "state"
    log = tmp_path / "actions.log"
    _run("acquire", "--owner-pid", _DEAD_PID, state_dir=state, action_log=log)
    log.write_text("")
    _run("release", "--owner-pid", _ALIVE_PID, state_dir=state, action_log=log)
    lines = log.read_text().splitlines()
    assert "zramctl --reset /dev/zram-dryrun" in lines
    assert not (state / "tokens" / _DEAD_PID).exists()


# --------------------------------------------------------------------------- #
# Argument boundary (the script is a NOPASSWD sudo target)
# --------------------------------------------------------------------------- #

def test_unknown_action_is_refused(tmp_path):
    # Security boundary: only acquire/release/status are valid. Anything else must
    # exit 2 and do nothing, keeping the NOPASSWD grant narrow.
    log = tmp_path / "actions.log"
    proc, lines = _run("rm-rf", state_dir=tmp_path / "state", action_log=log)
    assert proc.returncode == 2
    assert lines == []


def test_non_numeric_owner_pid_is_refused(tmp_path):
    # The owner PID becomes a filename under the root-owned token dir, so a value
    # like "../../etc/x" must be rejected (exit 2) before any privileged action --
    # this is a path-traversal boundary on the sudo grant. A regression that
    # dropped the numeric check would let acquire write outside the token dir.
    log = tmp_path / "actions.log"
    proc, lines = _run("acquire", "--owner-pid", "../../etc/evil",
                       state_dir=tmp_path / "state", action_log=log)
    assert proc.returncode == 2
    assert lines == []

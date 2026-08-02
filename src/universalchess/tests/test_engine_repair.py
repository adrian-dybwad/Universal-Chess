"""Tests for net-backed engine health and in-place repair (Maia weights).

Background / why these tests exist
----------------------------------
Maia installs as ``engines/maia/lc0`` plus a ``engines/maia/maia_weights/``
directory of neural-net files. A install whose weight download silently failed
(the historical raw/main 404) left the binary present but the weight dir empty.
Because installed-ness was decided solely by the binary, such a Maia reported as
"installed" and was offered for play, yet lc0 has no network to load and fails at
move time -- a silently broken engine, with only an empty "Default" profile.

These tests pin the two mechanisms that close that gap:

* Health: ``has_required_nets`` / ``is_usable`` / ``needs_repair`` / ``can_repair``
  distinguish "binary present" from "binary present AND nets present", and
  ``is_available`` (what gates offering an engine for play) now follows
  usability so a weightless Maia is not silently offered.
* Repair: ``repair_engine`` runs the engine's ``repair_commands`` (for Maia, the
  weights-only download) and then VERIFIES the nets are present, so a repair that
  did not actually fetch nets is reported as a failure rather than a false
  success.
"""

import stat

import pytest

from universalchess import paths
from universalchess.managers.engine_manager import EngineManager, ENGINES


def _make_executable_at(path):
    """Create parent dirs, an empty file at path, and mark it executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_maia_binary_only(engines_dir):
    """Place only Maia's lc0 binary (no weights) -- the broken install state."""
    _make_executable_at(engines_dir / "maia" / "lc0")


# The full set of Maia ELO nets the catalog declares as expected. Kept here as
# the test's own source of truth so a drift between the catalog and this list is
# caught by test_maia_declares_required_net rather than passing silently.
_MAIA_EXPECTED_NETS = frozenset(
    f"maia-{elo}.pb.gz" for elo in range(1100, 2000, 100)
)


def _add_maia_net(engines_dir, name="maia-1500.pb.gz"):
    """Drop one Maia net into the weights dir so the install becomes usable."""
    net_dir = engines_dir / "maia" / "maia_weights"
    net_dir.mkdir(parents=True, exist_ok=True)
    (net_dir / name).write_bytes(b"net")


def _add_maia_nets(engines_dir, names):
    """Drop the given set of Maia nets into the weights dir."""
    for name in names:
        _add_maia_net(engines_dir, name=name)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def test_maia_declares_required_net():
    """Maia must declare its required companion net in the catalog.

    Why: has_required_nets/needs_repair and the profile editor's net picker all
    derive from this single declaration. If it is dropped, a weightless Maia is
    no longer detectable as broken.

    How the regression manifests: required_net is None, so has_required_nets is
    trivially True and needs_repair can never fire -- the broken state goes
    undetected again.
    """
    net = ENGINES["maia"].required_net
    assert net is not None
    assert net.option_name == "WeightsFile"
    assert net.subdir == "maia/maia_weights"
    assert net.glob == "*.pb.gz"
    # The expected set drives the "top up the missing ones" affordance: it is the
    # difference between "usable (>=1 net)" and "complete". If it drifts from the
    # nets build-maia.sh actually fetches, missing_nets would report phantom or
    # miss real gaps, so pin it to the known 1100..1900 ladder.
    assert net.expected_files == _MAIA_EXPECTED_NETS


def test_has_required_nets_false_when_weight_dir_empty(tmp_path):
    """A Maia binary with no nets on disk has no required nets present.

    Why: this is the exact broken state (lc0 installed, weight dir empty). It
    must be detectable.

    How the regression manifests: has_required_nets returns True despite zero
    net files, so the engine is treated as healthy.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.has_required_nets("maia") is False


def test_has_required_nets_true_when_a_net_present(tmp_path):
    """One matching net file makes the required-net check pass.

    Why: the check is "at least one net", so a single downloaded net is enough
    to make Maia usable.

    How the regression manifests: the glob does not match the real net location
    (e.g. wrong subdir), so a present net is not found and the engine looks
    broken forever.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    _add_maia_net(engines_dir)
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.has_required_nets("maia") is True


def test_has_required_nets_true_for_engine_without_requirement(tmp_path):
    """An engine that declares no required net always passes the check.

    Why: the net check must not penalize ordinary single-binary engines (they
    have nothing to require); only net-backed engines gate on it.

    How the regression manifests: has_required_nets returns False for a normal
    engine, breaking is_usable/is_available for every non-net engine.
    """
    engines_dir = tmp_path / "engines"
    manager = EngineManager(engines_dir=str(engines_dir))

    assert ENGINES["berserk"].required_net is None
    assert manager.has_required_nets("berserk") is True


def test_weightless_maia_installed_but_not_usable_or_available(tmp_path, monkeypatch):
    """A binary-only Maia is installed but neither usable nor available for play.

    Why: is_installed (binary present) must stay True so the management UI still
    shows Maia and can offer Repair, but is_usable/is_available must be False so
    the play picker does not silently offer an engine that fails at move time.

    How the regression manifests: is_available returns True for a weightless
    Maia, so it is offered for play and lc0 dies with no network at game start.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    monkeypatch.setattr(paths, "ENGINES_DIR", str(engines_dir))
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.is_installed("maia") is True
    assert manager.is_usable("maia") is False
    assert manager.is_available("maia") is False
    assert manager.needs_repair("maia") is True
    assert manager.can_repair("maia") is True
    # With zero nets on disk, every expected net is missing.
    assert manager.missing_nets("maia") == _MAIA_EXPECTED_NETS


def test_maia_with_one_net_is_usable_available_and_not_broken(tmp_path, monkeypatch):
    """One net makes Maia usable/available and no longer BROKEN (needs_repair).

    Why: complements the broken-state test -- a Maia with at least one net must
    recover its usable/available status and drop the alarming needs_repair flag.
    It may still be incomplete (top-up available); that is asserted separately.

    How the regression manifests: needs_repair stays True or is_available stays
    False after a net is present, so a usable Maia is wrongly flagged broken.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    _add_maia_net(engines_dir)
    monkeypatch.setattr(paths, "ENGINES_DIR", str(engines_dir))
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.is_usable("maia") is True
    assert manager.is_available("maia") is True
    assert manager.needs_repair("maia") is False


def test_partial_maia_is_usable_but_can_top_up(tmp_path, monkeypatch):
    """8 of 9 nets: usable and not broken, but can top up the one missing net.

    Why: this is the exact state a best-effort weight download leaves when a
    single net (e.g. maia-1300) transiently fails -- Maia plays fine at the ELOs
    it has, so it must NOT be flagged needs_repair, yet the user must still be
    able to fetch the straggler. can_repair drives that optional top-up action;
    missing_nets names precisely which net to fetch.

    How the regression manifests: with the old "can_repair == needs_repair"
    coupling, can_repair is False for a usable-but-incomplete Maia, so the
    straggler net can never be fetched from the UI once >=1 net exists.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    present = _MAIA_EXPECTED_NETS - {"maia-1300.pb.gz"}
    _add_maia_nets(engines_dir, present)
    monkeypatch.setattr(paths, "ENGINES_DIR", str(engines_dir))
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.is_usable("maia") is True
    assert manager.is_available("maia") is True
    assert manager.needs_repair("maia") is False
    assert manager.can_repair("maia") is True
    assert manager.missing_nets("maia") == {"maia-1300.pb.gz"}
    assert manager.present_nets("maia") == present


def test_complete_maia_needs_no_repair_and_cannot_top_up(tmp_path, monkeypatch):
    """A full net set drops both the repair and the top-up affordances.

    Why: with every expected net present there is nothing to fetch, so neither
    the alarming Repair nor the quiet top-up should be offered.

    How the regression manifests: can_repair stays True on a complete install,
    so the UI perpetually offers a "download 0 missing weights" no-op.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    _add_maia_nets(engines_dir, _MAIA_EXPECTED_NETS)
    monkeypatch.setattr(paths, "ENGINES_DIR", str(engines_dir))
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.is_usable("maia") is True
    assert manager.needs_repair("maia") is False
    assert manager.can_repair("maia") is False
    assert manager.missing_nets("maia") == frozenset()


def test_uninstalled_maia_does_not_need_repair(tmp_path):
    """A Maia that is not installed at all is not a repair candidate.

    Why: needs_repair means "installed but incomplete". A missing binary is a
    fresh-install case, not a repair case; conflating them would show a Repair
    button where only Install applies.

    How the regression manifests: needs_repair returns True with no binary
    present, so the UI offers Repair for an engine that was never installed.
    """
    engines_dir = tmp_path / "engines"
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.is_installed("maia") is False
    assert manager.needs_repair("maia") is False
    assert manager.can_repair("maia") is False


def test_non_net_engine_never_needs_repair(tmp_path):
    """An ordinary engine (no required net) is never a repair candidate.

    Why: repair applies only to engines that declare companion files to fetch;
    single-binary engines have nothing to repair.

    How the regression manifests: needs_repair/can_repair return True for a
    normal installed engine, offering a nonsensical Repair action.
    """
    engines_dir = tmp_path / "engines"
    _make_executable_at(engines_dir / "berserk")
    manager = EngineManager(engines_dir=str(engines_dir))

    assert manager.is_installed("berserk") is True
    assert manager.needs_repair("berserk") is False
    assert manager.can_repair("berserk") is False


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def test_repair_engine_succeeds_and_makes_maia_usable(tmp_path, monkeypatch):
    """repair_engine runs the repair command and reports success once nets exist.

    Why: this is the self-heal path -- fetch the missing nets into the existing
    install (no rebuild). It must return True only after the nets are actually
    present, and the engine must become usable.

    How the regression manifests: repair_engine returns True while the weight dir
    is still empty (no post-repair verification), so the UI reports a fixed Maia
    that still cannot play.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    monkeypatch.setattr(paths, "ENGINES_DIR", str(engines_dir))
    manager = EngineManager(engines_dir=str(engines_dir))

    # Stand in for build-maia.sh --weights-only: the real command downloads nets
    # into the install dir. Simulate a successful download by creating one net,
    # returning (returncode, tail) like the real build-command runner.
    def fake_run_monitored_command(cmd, cwd, on_line, **kwargs):
        assert "--weights-only" in cmd
        _add_maia_net(engines_dir)
        on_line("downloaded maia-1500.pb.gz")
        return 0, ""

    monkeypatch.setattr(manager, "_run_monitored_command", fake_run_monitored_command)

    stages = []
    ok = manager.repair_engine(
        "maia",
        stage_callback=lambda stage, msg, frac, **kwargs: stages.append((stage, msg)),
    )

    assert ok is True
    assert manager.has_required_nets("maia") is True
    assert manager.is_available("maia") is True
    # Progress was reported (so the web bar/banner move during repair).
    assert len(stages) > 0


def test_repair_engine_fails_when_command_fails(tmp_path, monkeypatch):
    """A failing repair command makes repair_engine report failure with detail.

    Why: a network failure or wrong URL must surface as a clear repair failure,
    not a silent no-op, so the user knows to retry.

    How the regression manifests: repair_engine returns True (or swallows the
    non-zero exit), leaving the user believing the repair worked.
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    manager = EngineManager(engines_dir=str(engines_dir))

    def fake_run_monitored_command(cmd, cwd, on_line, **kwargs):
        return 1, "wget: unable to resolve host github.com"

    monkeypatch.setattr(manager, "_run_monitored_command", fake_run_monitored_command)

    ok = manager.repair_engine("maia")

    assert ok is False
    assert manager.get_install_error() is not None
    assert "unable to resolve host" in manager.get_install_error()


def test_repair_engine_fails_when_nets_still_missing(tmp_path, monkeypatch):
    """A command that exits 0 but fetches no nets is still a repair failure.

    Why: exit 0 is not proof the nets arrived. The post-repair verification is
    what prevents reporting a false success on a still-broken install.

    How the regression manifests: repair_engine returns True after a zero-exit
    command that produced no nets, so needs_repair stays True but the UI says
    "repaired".
    """
    engines_dir = tmp_path / "engines"
    _install_maia_binary_only(engines_dir)
    manager = EngineManager(engines_dir=str(engines_dir))

    def fake_run_monitored_command(cmd, cwd, on_line, **kwargs):
        return 0, ""  # "succeeds" but creates no net

    monkeypatch.setattr(manager, "_run_monitored_command", fake_run_monitored_command)

    ok = manager.repair_engine("maia")

    assert ok is False
    assert manager.get_install_error() is not None
    assert manager.needs_repair("maia") is True


def test_repair_engine_rejects_engine_without_repair_support(tmp_path):
    """Repair is refused for an engine that declares no repair_commands.

    Why: only net-backed engines (Maia) can be repaired in place. A normal engine
    has no repair procedure, so the call must fail cleanly rather than run an
    empty command list and report success.

    How the regression manifests: repair_engine returns True for an engine with
    no repair_commands (empty loop), implying a repair happened when none did.
    """
    engines_dir = tmp_path / "engines"
    _make_executable_at(engines_dir / "berserk")
    manager = EngineManager(engines_dir=str(engines_dir))

    ok = manager.repair_engine("berserk")

    assert ok is False
    assert manager.get_install_error() is not None

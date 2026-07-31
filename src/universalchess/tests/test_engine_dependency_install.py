"""Tests for build-dependency verification in the source installer.

A failed ``apt-get install`` of a build dependency used to only log a warning and
continue, so the missing tool surfaced much later as a cryptic compiler/Makefile
error from the *build* step (Ethereal/Demolito "clang: not found", Arasan "No 'bc'
found") instead of an actionable failure. These tests pin that the installer now
verifies the packages are actually present and aborts with a clear message when
they are not -- before cloning or building.
"""

from pathlib import Path

import pytest

from universalchess.managers.engine_manager import (
    ENGINE_DEPS_HELPER,
    EngineDefinition,
    EngineManager,
)
from universalchess.services.apt_recovery import RecoveryOutcome


def _stub_recovery(monkeypatch, outcome=RecoveryOutcome.PROCEEDED):
    """Replace the dpkg interrupted-transaction recovery with a fixed outcome.

    The dependency-gate tests exercise the apt step, not dpkg recovery; without
    this the source installer would invoke the real recovery (a live ``sudo
    dpkg`` on a Debian CI host). Defaulting to PROCEEDED keeps them focused on the
    dependency logic; passing DEFERRED_RESTART drives the self-restart path.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.apt_recovery.recover_interrupted_dpkg",
        lambda *a, **k: outcome,
    )


def _stub_fix_broken(monkeypatch, outcome=RecoveryOutcome.FAILED):
    """Replace the ``apt-get install -f`` repair with a fixed outcome.

    attempt_fix_broken() shells out to ``sudo apt-get install -f -y``. Left
    un-mocked, a test that forces packages to stay missing would both run real
    apt on the host and take an environment-dependent branch: on a Debian CI
    runner the repair succeeds (PROCEEDED) and the installer retries the
    dependency install -- a second apt call -- while on a dev box without apt it
    FAILS and does not retry. That divergence made the apt-call count (and the
    test) pass locally but fail on CI. Pinning the outcome mocks the boundary and
    keeps the branch deterministic.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.apt_recovery.attempt_fix_broken",
        lambda *a, **k: outcome,
    )


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _is_apt_install(cmd) -> bool:
    """True for the dependency install.

    The installer no longer runs ``sudo apt-get install`` directly; it invokes
    the pinned ``uc-engine-deps`` helper via ``sudo -n``, which is what the
    sudoers grant authorizes on a board without blanket passwordless sudo. The
    index refresh moved inside that helper, so there is no separate apt-get
    update command to recognize here.
    """
    return (
        isinstance(cmd, list)
        and cmd[:2] == ["sudo", "-n"]
        and Path(cmd[2]).name == ENGINE_DEPS_HELPER.name
    )


def _make_engine(deps):
    """A throwaway source-built engine carrying the given apt dependencies."""
    return EngineDefinition(
        name="dummy",
        display_name="Dummy",
        summary="",
        description="",
        repo_url="https://example.invalid/dummy.git",
        build_commands=["true"],
        binary_path="dummy",
        is_system_package=False,
        package_name=None,
        extra_files=[],
        dependencies=deps,
    )


def test_missing_packages_reports_uninstalled(monkeypatch):
    """Only packages dpkg does not report installed are returned.

    Why this test exists: the abort decision hinges on correctly reading the local
    package state. git is installed, bc is not (dpkg-query exits non-zero for an
    unknown package), so only bc must be reported missing.

    How the regression manifests: if the status string check is loosened (e.g. just
    checking the return code, or substring-matching "installed" anywhere), bc would
    wrongly be treated as present and the cryptic build error would return.
    """
    def fake_run(cmd, **kwargs):
        pkg = cmd[-1]
        if pkg == "git":
            return _FakeProc(returncode=0, stdout="install ok installed")
        return _FakeProc(returncode=1, stdout="")  # unknown package

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.subprocess.run", fake_run
    )
    assert EngineManager._missing_packages(["git", "bc"]) == ["bc"]


def test_missing_packages_treats_half_configured_as_missing(monkeypatch):
    """A half-configured package counts as missing, not present.

    Why this test exists: a package can be unpacked but not configured (dpkg exits
    0 with status "install ok half-configured"). The build cannot rely on such a
    package, so it must count as missing. This is exactly the state that wedged apt
    on a field board and led to the original Arasan bc failure.

    How the regression manifests: matching on return code alone (0) would accept a
    half-configured package as installed.
    """
    def fake_run(cmd, **kwargs):
        return _FakeProc(returncode=0, stdout="install ok half-configured")

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.subprocess.run", fake_run
    )
    assert EngineManager._missing_packages(["foo"]) == ["foo"]


def test_install_from_source_aborts_when_dependency_missing(monkeypatch, tmp_path):
    """The build aborts (no clone) when a declared dependency stays uninstalled.

    Why this test exists: this is the core fix. apt "succeeds" (returns 0) but the
    package is still absent -- the real-world case where apt is wedged by a
    half-configured package. The installer must stop with an error naming the
    missing package instead of cloning and hitting a cryptic build failure.

    How the regression manifests: reverting to the old warn-and-continue behavior
    lets execution reach the git clone (the sentinel below raises) and returns a
    later, cryptic build error rather than the early, clear dependency error.
    """
    _stub_recovery(monkeypatch)
    # fix-broken FAILS (its remedy did not help), so there is no retry and the
    # single apt attempt is asserted below. Mocked so this does not depend on the
    # host actually having apt (see _stub_fix_broken).
    _stub_fix_broken(monkeypatch, RecoveryOutcome.FAILED)
    manager = EngineManager(engines_dir=str(tmp_path))
    manager.build_tmp = Path(tmp_path) / "build"
    engine = _make_engine(["build-essential", "git", "bc"])

    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if _is_apt_install(cmd):
            return _FakeProc(returncode=0, stdout="", stderr="")
        raise AssertionError(f"build must not proceed past missing deps; ran: {cmd}")

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.subprocess.run", fake_run
    )
    # bc remains missing after the (mocked) apt install.
    monkeypatch.setattr(manager, "_missing_packages", lambda pkgs: ["bc"])

    result = manager._install_from_source(engine, lambda *a, **k: None)

    assert result is False
    assert manager._install_error is not None
    assert "bc" in manager._install_error
    # Aborted before cloning: exactly one dependency install ran, no git clone.
    installs = [c for c in seen if _is_apt_install(c)]
    assert len(installs) == 1
    assert seen == installs


def test_install_from_source_retries_apt_once_after_fix_broken_then_aborts(monkeypatch, tmp_path):
    """A successful fix-broken triggers exactly one apt retry, then aborts if still missing.

    Why this test exists: apt can abort because the system already holds broken
    packages ("Try 'apt --fix-broken install'"). The installer runs that remedy
    and, when it PROCEEDs, retries the dependency install once. If the package is
    still absent it must abort before cloning rather than loop. This is the exact
    branch whose environment-dependent behavior (apt present on CI vs. absent
    locally) surfaced as a CI-only failure when attempt_fix_broken was left
    un-mocked, so it is pinned deterministically here.

    How the regression manifests: dropping the retry would show one apt call;
    looping would show more than two; proceeding to clone would hit the sentinel.
    """
    _stub_recovery(monkeypatch)
    _stub_fix_broken(monkeypatch, RecoveryOutcome.PROCEEDED)
    manager = EngineManager(engines_dir=str(tmp_path))
    manager.build_tmp = Path(tmp_path) / "build"
    engine = _make_engine(["build-essential", "git", "bc"])

    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if _is_apt_install(cmd):
            return _FakeProc(returncode=0, stdout="", stderr="")
        raise AssertionError(f"build must not proceed past missing deps; ran: {cmd}")

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.subprocess.run", fake_run
    )
    monkeypatch.setattr(manager, "_missing_packages", lambda pkgs: ["bc"])

    result = manager._install_from_source(engine, lambda *a, **k: None)

    assert result is False
    assert manager._install_error is not None and "bc" in manager._install_error
    # One initial dependency install plus one retry after fix-broken, and no clone.
    installs = [c for c in seen if _is_apt_install(c)]
    assert len(installs) == 2
    # No other command (e.g. a git clone) was reached.
    assert seen == installs


def test_helper_refreshes_apt_index_before_installing_deps():
    """The dependency helper runs apt-get update before apt-get install.

    Why this test exists: source dependencies like Zahak's ``golang`` are
    metapackages with versioned inter-dependencies (golang -> golang-1.24 ->
    golang-1.24-go (>= X)). Against a stale package index apt cannot satisfy the
    version constraint and aborts with "unmet dependencies ... not going to be
    installed" -- the observed Zahak failure -- which ``apt --fix-broken`` cannot
    repair because nothing is actually broken. Refreshing the index first is the
    real remedy.

    The refresh used to be a separate ``sudo apt-get update`` issued by the
    installer; it now lives inside the pinned helper, because the installer no
    longer has (or wants) a sudo grant for apt itself. This asserts at the level
    the behavior moved to, so the remedy stays pinned rather than silently
    disappearing with the call site.

    How the regression manifests: dropping the refresh from the helper leaves
    only an install command in the script, and the stale-index resolution failure
    can recur on a board whose index predates the current golang version.
    """
    # Comment lines are excluded deliberately: the header explains why apt is not
    # granted directly and mentions "apt-get install" in prose, which a naive
    # whole-text search matches before the real command and inverts the ordering
    # assertion.
    commands = [
        line for line in ENGINE_DEPS_HELPER.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    update_lines = [i for i, line in enumerate(commands) if "apt-get update" in line]
    install_lines = [i for i, line in enumerate(commands) if "apt-get install" in line]

    assert update_lines, "helper does not refresh the apt index"
    assert install_lines, "helper does not install anything"
    assert update_lines[0] < install_lines[0], (
        "the index refresh must precede the install"
    )


def test_install_from_source_continues_when_dependencies_present(monkeypatch, tmp_path):
    """With all deps present, the build proceeds past the dependency stage.

    Why this test exists: the abort must be specific to missing packages and not
    block normal installs. With nothing missing, execution must reach the clone
    step (the sentinel below confirms it got there).

    How the regression manifests: an over-broad abort (e.g. treating any apt
    non-zero exit as fatal) would stop here even though every dependency is
    installed, breaking installs on boards where apt returns non-zero for benign
    trigger reasons.
    """
    _stub_recovery(monkeypatch)
    manager = EngineManager(engines_dir=str(tmp_path))
    manager.build_tmp = Path(tmp_path) / "build"
    engine = _make_engine(["build-essential", "git"])

    reached_clone = {"value": False}

    def fake_run(cmd, **kwargs):
        if _is_apt_install(cmd):
            return _FakeProc(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and cmd[:2] == ["git", "clone"]:
            reached_clone["value"] = True
            # Stop the test here; we only care that the dependency gate passed.
            raise RuntimeError("stop after reaching clone")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.subprocess.run", fake_run
    )
    monkeypatch.setattr(manager, "_missing_packages", lambda pkgs: [])

    # The fake clone raises this sentinel to stop the build once the dependency
    # gate has passed; expecting it here keeps the "reached clone" intent explicit.
    with pytest.raises(RuntimeError, match="stop after reaching clone"):
        manager._install_from_source(engine, lambda *a, **k: None)

    assert reached_clone["value"] is True


def test_install_from_source_aborts_with_friendly_message_on_deferred_restart(
    monkeypatch, tmp_path
):
    """When recovery must restart the service, abort with a user-facing warning.

    Why this test exists: if dpkg recovery finds universal-chess itself
    half-configured, ``dpkg --configure -a`` is launched out-of-process and will
    restart this service. The install cannot complete, so the installer must stop
    BEFORE touching apt and set a plain-language message telling the user to retry
    after the restart -- never a console instruction, and never a half-run apt.

    How the regression manifests: if the deferred-restart outcome were ignored,
    execution would fall through to ``sudo apt-get install`` (the sentinel below
    raises) -- running apt into a transaction that is about to be killed by the
    restart -- and no actionable message would reach the UI.
    """
    _stub_recovery(monkeypatch, RecoveryOutcome.DEFERRED_RESTART)
    manager = EngineManager(engines_dir=str(tmp_path))
    manager.build_tmp = Path(tmp_path) / "build"
    engine = _make_engine(["build-essential", "git"])

    def fake_run(cmd, **kwargs):
        raise AssertionError(f"apt/clone must not run when a restart is imminent; ran: {cmd}")

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.subprocess.run", fake_run
    )

    result = manager._install_from_source(engine, lambda *a, **k: None)

    assert result is False
    assert manager._install_error is not None
    # The message must be plain-language and reference retrying after restart,
    # not a console command.
    assert "Fixing incomplete install of Universal Chess" in manager._install_error
    assert "install Dummy" in manager._install_error
    assert "after the service restarts" in manager._install_error
    assert "dpkg" not in manager._install_error.lower()

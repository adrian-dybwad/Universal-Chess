"""Tests for build-dependency verification in the source installer.

A failed ``apt-get install`` of a build dependency used to only log a warning and
continue, so the missing tool surfaced much later as a cryptic compiler/Makefile
error from the *build* step (Ethereal/Demolito "clang: not found", Arasan "No 'bc'
found") instead of an actionable failure. These tests pin that the installer now
verifies the packages are actually present and aborts with a clear message when
they are not -- before cloning or building.
"""

from pathlib import Path

from universalchess.managers.engine_manager import EngineManager, EngineDefinition


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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
    manager = EngineManager(engines_dir=str(tmp_path))
    manager.build_tmp = Path(tmp_path) / "build"
    engine = _make_engine(["build-essential", "git", "bc"])

    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if isinstance(cmd, str) and cmd.startswith("sudo apt-get install"):
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
    # Aborted before cloning: the only subprocess invoked is the apt install.
    assert len(seen) == 1
    assert isinstance(seen[0], str) and seen[0].startswith("sudo apt-get install")


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
    manager = EngineManager(engines_dir=str(tmp_path))
    manager.build_tmp = Path(tmp_path) / "build"
    engine = _make_engine(["build-essential", "git"])

    reached_clone = {"value": False}

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, str) and cmd.startswith("sudo apt-get install"):
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

    try:
        manager._install_from_source(engine, lambda *a, **k: None)
    except RuntimeError:
        pass

    assert reached_clone["value"] is True

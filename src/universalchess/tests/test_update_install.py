"""Tests for the update install architecture.

These guard the install path that was the source of the "stuck on installing"
bug. The core invariant: the install command must run in a transient systemd
unit, never inline in a service cgroup, because the package postinst restarts
both services (KillMode=control-group) and would otherwise kill the installer
mid-install.

A second invariant guards dependency handling: the install must go through
apt (which resolves dependencies atomically), not `dpkg -i`. `dpkg -i` is
dependency-unaware and, on a missing dependency, leaves the package
half-configured -- the failure mode that bricked an update when the mkcert /
libnss3-tools dependencies were added.

Each test documents the specific regression it guards and how that regression
would manifest if the install path reverted to running the installer inline,
in a setsid child, or via bare dpkg.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import universalchess.services.update_service as us
from universalchess.services.update_service import UpdateService, INSTALL_UNIT


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Build an UpdateService with all on-disk paths redirected to tmp_path.

    subprocess.run is stubbed during construction so get_current_version()
    (which shells out to dpkg-query) does not touch the real system.
    """
    state_file = tmp_path / "update-state.json"
    version_file = tmp_path / "VERSION"
    pending_dir = tmp_path / "pending-updates"
    monkeypatch.setattr(us, "STATE_FILE", state_file)
    monkeypatch.setattr(us, "VERSION_FILE", version_file)
    monkeypatch.setattr(us, "PENDING_DEB_DIR", pending_dir)
    version_file.write_text("2.0.0-nightly")

    with patch.object(us.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="2.0.0-nightly", stderr="")
        svc = UpdateService()
    return svc


def _make_deb(tmp_path) -> Path:
    deb = tmp_path / "pending-updates" / "universal-chess_new_all.deb"
    deb.parent.mkdir(parents=True, exist_ok=True)
    deb.write_bytes(b"fake deb")
    return deb


class TestInstallLaunchesTransientUnit:
    """install_update must launch dpkg via systemd-run, not inline."""

    def test_launches_systemd_run_with_collect_and_unit(self, service, tmp_path):
        """The install must be started with systemd-run, a fixed --unit name,
        and --collect. Regression: if this reverts to inline `dpkg -i`, the
        postinst's service restart kills dpkg mid-install and the package is
        left half-configured (the exact "stuck on installing" bug).
        """
        deb = _make_deb(tmp_path)

        with patch.object(us.subprocess, "run") as mock_run:
            # is_installing() check (systemctl is-active) -> not active,
            # then the systemd-run launch -> success.
            mock_run.side_effect = [
                MagicMock(returncode=3, stdout="inactive", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            ok = service.install_update(deb)

        assert ok is True
        launch_argv = mock_run.call_args_list[-1][0][0]
        assert launch_argv[0] == "sudo"
        assert "systemd-run" in launch_argv
        assert "--collect" in launch_argv
        assert f"--unit={INSTALL_UNIT}" in launch_argv
        # The script path is the final argument and must be an existing file.
        script_path = Path(launch_argv[-1])
        assert script_path.exists()

    def test_generated_script_installs_via_apt_with_dep_resolution(self, service, tmp_path):
        """The install script must install the .deb through apt so the
        package's dependencies are resolved in one transaction. Regression:
        reverting to bare `dpkg -i` means a .deb whose deps changed (e.g. the
        mkcert/libnss3-tools deps) unpacks but fails to configure, leaving the
        package half-configured and the board bricked.

        The forcing flags are asserted individually because each is load-
        bearing: without -f apt refuses to proceed when a prior failed install
        left the system with unmet dependencies; without --reinstall apt
        no-ops (every nightly shares the dpkg version "2.0.0-nightly");
        without --allow-downgrades a channel switch to a lower-sorting version
        is refused.
        """
        deb = _make_deb(tmp_path)
        script = service._build_install_script(deb)

        assert f'DEB="{deb}"' in script
        assert "apt-get install" in script
        assert "-f" in script.split("apt-get install", 1)[1].split("\n", 1)[0]
        assert "--reinstall" in script
        assert "--allow-downgrades" in script
        # Bare dpkg -i is the dependency-unaware path that caused the bug.
        assert "dpkg -i" not in script
        # State must be cleared after a successful install so the board does
        # not try to reinstall the same .deb on the next shutdown.
        assert '"pending_deb"' in script
        assert '"available_version"' in script

    def test_script_does_not_prestop_or_restart_services(self, service, tmp_path):
        """The script must NOT stop the board service before dpkg, and must
        NOT restart services itself. Regression on pre-stop: stopping
        universal-chess.service from inside it (the original bug) kills the
        process before dpkg runs. Regression on manual restart: racing the
        postinst restart causes double restarts / interrupted installs.
        """
        deb = _make_deb(tmp_path)
        script = service._build_install_script(deb)

        assert "systemctl stop universal-chess.service" not in script
        assert "systemctl restart" not in script

    def test_script_does_not_use_sudo_internally(self, service, tmp_path):
        """The unit runs as root, so the script must not call sudo. Regression:
        sudo inside a non-interactive root unit can fail or hang waiting for a
        tty, aborting the install.
        """
        deb = _make_deb(tmp_path)
        script = service._build_install_script(deb)
        # Check for sudo as an invoked command (start of a stripped line),
        # not as a substring -- the temp deb path may itself contain "sudo".
        assert not any(line.strip().startswith("sudo ") for line in script.splitlines())


class TestInstallGuards:
    """install_update must refuse to run in unsafe / pointless conditions."""

    def test_returns_false_when_deb_missing(self, service, tmp_path):
        """A missing .deb must not launch an install. Regression: launching
        systemd-run with a nonexistent path would fail in the background with
        no user-visible error.
        """
        assert service.install_update(tmp_path / "nope.deb") is False

    def test_returns_false_when_already_installing(self, service, tmp_path):
        """A second install must be refused while one is active. Regression:
        concurrent dpkg runs corrupt the dpkg database. is_installing() is
        driven by the transient unit so this holds across processes.
        """
        deb = _make_deb(tmp_path)
        with patch.object(service, "is_installing", return_value=True):
            assert service.install_update(deb) is False

    def test_returns_false_when_systemd_run_fails(self, service, tmp_path):
        """If systemd-run itself fails (e.g. unit already exists), the launch
        must report failure. Regression: returning success here makes the UI
        claim an install started when nothing is running.
        """
        deb = _make_deb(tmp_path)
        with patch.object(us.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=3, stdout="inactive", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="unit already exists"),
            ]
            assert service.install_update(deb) is False


class TestIsInstalling:
    """is_installing must reflect the transient unit's state."""

    def test_true_when_unit_active(self, service):
        """When the install unit is active, is_installing must be True.
        Regression: relying only on an in-memory flag reports False in the
        process that did not launch the install (web vs board).
        """
        with patch.object(us.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
            assert service.is_installing() is True

    def test_false_when_unit_inactive(self, service):
        """When the unit is not active, is_installing must be False so the UI
        can leave the 'installing' state. Regression: a stuck True leaves the
        UI spinning forever (the reported 'stuck on updating' symptom).
        """
        with patch.object(us.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3, stdout="inactive\n", stderr="")
            assert service.is_installing() is False


class TestInstallPending:
    """install_pending_update routes through install_update."""

    def test_returns_false_without_pending(self, service):
        """No pending .deb must not launch anything. Regression: installing a
        null path would crash the background unit.
        """
        assert service.install_pending_update() is False

    def test_installs_pending_deb(self, service, tmp_path):
        """A present pending .deb must be handed to install_update. Regression:
        a broken hand-off leaves a downloaded update that can never install.
        """
        deb = _make_deb(tmp_path)
        service._state.pending_deb = str(deb)

        with patch.object(service, "install_update", return_value=True) as mock_install:
            assert service.install_pending_update() is True
            mock_install.assert_called_once_with(deb)


class TestInstallLocalDeb:
    """install_local_deb validates the path then routes to install_update."""

    def test_missing_file_returns_false(self, service):
        """A missing local .deb must return False without launching. Regression:
        passing a bad path to systemd-run fails silently in the background.
        """
        assert service.install_local_deb("/does/not/exist.deb") is False


class TestNightlyVersionComparison:
    """_is_newer must treat an installed nightly (recorded by its release tag)
    as current, so "check for updates" does not perpetually report an update.

    The bug these guard: the build wrote the dpkg version "2.0.0-nightly" into
    the VERSION file instead of the release tag. Because that string is not a
    "nightly-..." tag, every check reported an update available even when the
    board was running the latest nightly. The fix records the release tag in
    VERSION; these tests pin the comparison contract that fix depends on.
    """

    def test_same_nightly_tag_is_not_newer(self, service):
        """Identical nightly tags must compare as not-newer, i.e. up to date.
        Regression: if this returns True the UI always shows an update for the
        build that is already installed.
        """
        tag = "nightly-2026-06-17-f0e809a"
        assert service._is_newer(tag, tag) is False

    def test_later_nightly_date_is_newer(self, service):
        """A later-dated nightly tag must compare as newer. Regression: if this
        returns False, real nightly updates are never offered.
        """
        assert service._is_newer(
            "nightly-2026-06-18-aaaaaaa", "nightly-2026-06-17-f0e809a"
        ) is True

    def test_dpkg_version_string_breaks_nightly_comparison(self, service):
        """Documents WHY VERSION must hold the release tag, not the dpkg
        version: with the dpkg-style "2.0.0-nightly" as the current version,
        any nightly tag is reported as newer (the symptom of the original
        bug). This pins the rationale so the build-side fix is not reverted.
        """
        assert service._is_newer(
            "nightly-2026-06-17-f0e809a", "2.0.0-nightly"
        ) is True

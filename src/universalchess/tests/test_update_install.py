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

A third invariant guards privilege scoping: the service user cannot run apt or
systemd-run as root directly (that would be unrestricted root). It escalates
through a single pinned helper script that validates the .deb and runs the
install in the transient unit. The Python side must invoke exactly that helper
via sudo; the helper script itself must enforce the directory validation and
carry the apt/systemd-run logic.

Each test documents the specific regression it guards and how that regression
would manifest if the install path reverted to running the installer inline,
in a setsid child, via bare dpkg, or with a broad sudo grant.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import universalchess.services.update_service as us
from universalchess.services.update_service import (
    UpdateService,
    INSTALL_UNIT,
    INSTALL_HELPER,
)

# The pinned root helper, located relative to the update_service module so the
# test reads the same file that ships in the package
# (src/universalchess/scripts/install-update -> /opt/.../scripts/install-update).
HELPER_SCRIPT = Path(us.__file__).resolve().parent.parent / "scripts" / "install-update"


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


class TestInstallLaunchesViaHelper:
    """install_update must escalate through the pinned root helper, not by
    running systemd-run/apt under a broad sudo grant."""

    def test_launches_pinned_helper_with_deb(self, service, tmp_path):
        """The install must be launched as `sudo -n <helper> <deb>`. Regression:
        if this reverts to `sudo systemd-run ...`, the deployment needs a
        NOPASSWD grant on systemd-run (== unrestricted root) and the install
        fails entirely on boards that only granted the pinned helper -- the
        "cannot update" symptom. `-n` keeps a missing grant from hanging on a
        password prompt.
        """
        deb = _make_deb(tmp_path)

        with patch.object(us.subprocess, "run") as mock_run:
            # is_installing() check (systemctl is-active) -> not active,
            # then the helper launch -> success.
            mock_run.side_effect = [
                MagicMock(returncode=3, stdout="inactive", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            ok = service.install_update(deb)

        assert ok is True
        launch_argv = mock_run.call_args_list[-1][0][0]
        assert launch_argv == ["sudo", "-n", INSTALL_HELPER, str(deb)]


class TestHelperScriptContract:
    """The shipped helper script carries the privileged install logic. These
    read the actual file so the invariants cannot silently drift from the
    Python that invokes it."""

    @pytest.fixture
    def helper_text(self):
        """The helper must ship with the package; a missing file means the
        sudoers grant points at nothing and self-update is impossible.
        """
        assert HELPER_SCRIPT.exists(), f"helper script missing: {HELPER_SCRIPT}"
        return HELPER_SCRIPT.read_text()

    def test_runs_in_transient_unit(self, helper_text):
        """The helper must launch the install in a transient systemd unit with
        the fixed unit name and --collect. Regression: running the install in
        the caller's cgroup lets the postinst's KillMode=control-group restart
        kill it mid-install (the "stuck on installing" bug).
        """
        assert "systemd-run" in helper_text
        assert "--collect" in helper_text
        assert "--unit=" in helper_text
        # The unit name is bound to a shell variable; assert the value is
        # present so it stays in lockstep with INSTALL_UNIT on the Python side.
        assert INSTALL_UNIT in helper_text

    def test_installs_via_apt_with_dep_resolution(self, helper_text):
        """The helper must install through apt with the load-bearing forcing
        flags. Regression: bare `dpkg -i` is dependency-unaware and leaves a
        package whose deps changed (mkcert/libnss3-tools) half-configured;
        without -f apt refuses after a prior failed install; without --reinstall
        apt no-ops on the shared nightly version; without --allow-downgrades a
        channel switch to a lower version is refused.
        """
        assert "apt-get install" in helper_text
        apt_line = helper_text.split("apt-get install", 1)[1].split("\n", 1)[0]
        assert "-f" in apt_line
        assert "--reinstall" in apt_line
        assert "--allow-downgrades" in apt_line
        assert "dpkg -i" not in helper_text

    def test_recovers_interrupted_dpkg_before_apt_install(self, helper_text):
        """The helper must run `dpkg --configure -a` before the apt install.

        Regression (observed on a field board): an unrelated, previously
        interrupted dpkg transaction (e.g. a manual `apt build-dep` or install
        that was killed) leaves the dpkg database half-configured. Every
        subsequent apt operation then aborts during "Reading package lists"
        with "E: dpkg was interrupted, you must manually run 'dpkg --configure
        -a'", so the OTA install fails even though nothing is wrong with the
        update itself. `apt-get install -f` does NOT clear this state -- only
        `dpkg --configure -a` finishes the interrupted transaction. Running it
        first makes the installer self-heal instead of bricking the OTA.

        The ordering matters: configuring must happen BEFORE the apt install,
        otherwise apt aborts before the recovery runs.
        """
        assert "dpkg --configure -a" in helper_text
        configure_pos = helper_text.index("dpkg --configure -a")
        install_pos = helper_text.index("apt-get install")
        assert configure_pos < install_pos, (
            "dpkg --configure -a must run before apt-get install"
        )

    def test_validates_deb_lives_in_pending_dir(self, helper_text):
        """The helper must refuse a .deb outside the managed pending-updates
        directory. Regression: this directory check is the security boundary
        for the NOPASSWD grant -- without it, the pinned sudo entry would let
        the service user install an arbitrary file as root.
        """
        assert "/opt/universalchess/pending-updates" in helper_text
        # Canonicalize before matching so a '..' traversal cannot escape.
        assert "readlink -f" in helper_text
        assert "refusing to install outside" in helper_text

    def test_clears_pending_state_after_install(self, helper_text):
        """The helper must clear the pending marker after a successful install.
        Regression: leaving pending_deb set makes the board attempt to
        reinstall the same .deb on the next boot/shutdown.
        """
        assert '"pending_deb"' in helper_text
        assert '"available_version"' in helper_text

    def test_does_not_prestop_or_restart_services(self, helper_text):
        """The helper must NOT stop the board service before the install, and
        must NOT restart services itself. Regression on pre-stop: stopping
        universal-chess.service kills the caller before the install detaches.
        Regression on manual restart: racing the postinst restart causes double
        restarts / interrupted installs.
        """
        assert "systemctl stop universal-chess.service" not in helper_text
        assert "systemctl restart" not in helper_text

    def test_does_not_nest_sudo(self, helper_text):
        """The helper already runs as root (invoked via sudo), so it must not
        call sudo internally. Regression: sudo inside a non-interactive root
        context can fail or hang waiting for a tty, aborting the install.
        """
        assert not any(
            line.strip().startswith("sudo ") for line in helper_text.splitlines()
        )


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

    def test_returns_false_when_helper_fails(self, service, tmp_path):
        """If the helper exits non-zero (e.g. missing sudo grant, unit already
        exists, or validation rejected the .deb), the launch must report
        failure. Regression: returning success here makes the UI claim an
        install started when nothing is running.
        """
        deb = _make_deb(tmp_path)
        with patch.object(us.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=3, stdout="inactive", stderr=""),
                MagicMock(returncode=1, stdout="", stderr="sudo: a password is required"),
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


class TestStatusDict:
    """get_status_dict must report install progress from the transient unit.

    This is what the web/board poll reads. Reporting it from the systemd unit
    (not an in-memory flag) is what makes an in-flight install survive a page
    refresh and be visible in the web process, which did not launch the install.
    """

    def test_is_installing_true_when_unit_active(self, service):
        # Regression (the reported bug): get_status_dict previously returned the
        # in-memory _installing flag, which is never set True, so a refresh -- or
        # the web process -- saw is_installing False and re-showed the Install
        # button, and a second press collided with the still-running install.
        with patch.object(us.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
            assert service.get_status_dict()["is_installing"] is True

    def test_is_installing_false_when_unit_inactive(self, service):
        # When no install unit is active the status must report not-installing so
        # the UI leaves the in-progress state (and offers install again).
        with patch.object(us.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3, stdout="inactive\n", stderr="")
            assert service.get_status_dict()["is_installing"] is False


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

        The pending marker is persisted (not just set in memory) because the
        install path resolves it from disk -- see the cross-process test below.
        """
        deb = _make_deb(tmp_path)
        service._state.pending_deb = str(deb)
        service._save_state()

        with patch.object(service, "install_update", return_value=True) as mock_install:
            assert service.install_pending_update() is True
            mock_install.assert_called_once_with(deb)

    def test_pending_downloaded_by_other_process_is_visible(self, service, tmp_path):
        """A .deb staged by ANOTHER process (recorded only in the on-disk state,
        not this instance's memory) must be seen as pending and be installable.

        This is the auto-update path: the board process downloads and records the
        pending .deb, while the web process -- a separate UpdateService instance --
        serves the navbar indicator and the Install action. Regression: if these
        read the in-memory _state instead of disk, the web process's stale state
        (pending_deb=None) would hide the update, so the indicator never appears
        and Install refuses -- the feature would silently do nothing.
        """
        deb = _make_deb(tmp_path)
        # Simulate the other process: write the marker to disk WITHOUT touching
        # this instance's in-memory state.
        state_on_disk = us.UpdateState(pending_deb=str(deb))
        us.STATE_FILE.write_text(json.dumps(state_on_disk.to_dict()))
        assert service._state.pending_deb is None  # in-memory copy is stale

        assert service.has_pending_update() is True
        assert service.get_status_dict()["has_pending_update"] is True

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


class TestStartupUpdateCheck:
    """run_startup_update_check stages an update at boot without ever installing.

    The product decision for a chess appliance: auto-update NEVER installs on its
    own (a surprise restart could interrupt play). Startup only checks the channel
    and downloads the newest build in the background; a toolbar icon then signals
    the user, who installs it manually from Settings -> System. These tests pin
    that contract so a future change cannot silently reintroduce an auto-install.
    """

    def test_disabled_does_nothing(self, service):
        """With auto-update off, startup must not check, download, or install.
        Regression: if the gate is dropped, every boot would hit the network and
        stage updates the user never asked to auto-manage.
        """
        service._state.auto_update = False
        with patch.object(service, "check_and_download_async") as mock_dl, \
             patch.object(service, "install_pending_update") as mock_install:
            assert service.run_startup_update_check() == "disabled"
            mock_dl.assert_not_called()
            mock_install.assert_not_called()

    def test_enabled_without_pending_downloads_in_background(self, service):
        """Auto-update on, nothing staged yet: startup kicks off a background
        check+download so the update is ready to install later. Regression: if
        this did a blocking check, boot would stall on a slow/absent network; if
        it did nothing, auto-update would never stage anything.
        """
        service._state.auto_update = True
        service._state.pending_deb = None
        with patch.object(service, "check_and_download_async") as mock_dl, \
             patch.object(service, "install_pending_update") as mock_install:
            assert service.run_startup_update_check() == "checking"
            mock_dl.assert_called_once()
            mock_install.assert_not_called()

    def test_enabled_with_pending_does_not_reinstall_or_redownload(self, service, tmp_path):
        """Auto-update on with an update already downloaded: startup must NOT
        install it (no auto-install, ever) and must NOT re-download over it (the
        staged build is already waiting for the user). Regression: auto-installing
        here is exactly the surprise-restart behavior this feature removed; re-
        downloading would churn the pending .deb the user is about to install.
        """
        service._state.auto_update = True
        deb = _make_deb(tmp_path)
        service._state.pending_deb = str(deb)
        service._save_state()  # pending is resolved from disk (cross-process truth)
        with patch.object(service, "check_and_download_async") as mock_dl, \
             patch.object(service, "install_pending_update") as mock_install:
            assert service.run_startup_update_check() == "pending"
            mock_dl.assert_not_called()
            mock_install.assert_not_called()


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

    def test_same_date_different_sha_is_newer(self, service):
        """A same-day rebuild (same date, different commit sha) must compare as
        newer. The nightly pipeline keeps exactly one release per date -- it
        deletes the day's prior release before publishing a rebuild -- so a
        different sha for today's date is always the rebuilt latest, and the
        installed same-date build has been replaced.

        Regression (the observed "board does not think there is an update"):
        the comparison did a lexicographic string compare that, after the equal
        'nightly-2026-06-19-' prefix, compared the trailing short shas. The
        short sha has no chronological meaning, so a newer build whose sha sorts
        before the installed one ('0a6a09c' < '23a8f85') was judged OLDER and no
        update was offered.
        """
        assert service._is_newer(
            "nightly-2026-06-19-0a6a09c", "nightly-2026-06-19-23a8f85"
        ) is True

    def test_earlier_nightly_date_is_not_newer(self, service):
        """An earlier-dated nightly must not be offered (it would downgrade the
        board). Regression: a naive 'any different sha => newer' rule that
        ignored the date would roll the board back to an older build.
        """
        assert service._is_newer(
            "nightly-2026-06-18-aaaaaaa", "nightly-2026-06-19-23a8f85"
        ) is False

    def test_timestamped_same_day_later_time_is_newer(self, service):
        """The compact date+time format (nightly-YYYYMMDD-HHMMSS-sha) embeds the
        build time so two builds on the same day order by time. A later build
        time must be newer. Regression: if only the date were compared, same-day
        rebuilds would tie and fall back to the sha, which has no chronological
        order.
        """
        assert service._is_newer(
            "nightly-20260619-031500-0a6a09c", "nightly-20260619-010000-23a8f85"
        ) is True

    def test_dashed_datetime_same_day_later_time_is_newer(self, service):
        """The current tag format restores the dashed date but keeps the time
        (nightly-YYYY-MM-DD-HHMMSS-sha). A later build time on the same day must
        still be newer. Regression: if rpartition split the sha on the wrong dash
        (the date now contains dashes), the stamp would be mangled and same-day
        ordering would break.
        """
        assert service._is_newer(
            "nightly-2026-06-19-031500-0a6a09c", "nightly-2026-06-19-010000-23a8f85"
        ) is True

    def test_dashed_and_compact_datetime_same_instant_same_sha_not_newer(self, service):
        """Switching the workflow from the compact form (nightly-YYYYMMDD-...)
        to the dashed form (nightly-YYYY-MM-DD-...) must be a no-op for the same
        build: identical instant and sha differing only in date dashes parse to
        the same digit stamp and equal sha, so it is NOT newer. Regression: if
        the dashes leaked into the comparison, every field board would see a
        phantom update the moment the tag format changed.
        """
        assert service._is_newer(
            "nightly-2026-06-19-031500-0a6a09c", "nightly-20260619-031500-0a6a09c"
        ) is False

    def test_timestamped_same_day_earlier_time_is_not_newer(self, service):
        """An earlier build time on the same day must not be offered. Regression:
        a backwards time comparison would let the checker roll the board back to
        an earlier same-day build.
        """
        assert service._is_newer(
            "nightly-20260619-010000-23a8f85", "nightly-20260619-031500-0a6a09c"
        ) is False

    def test_new_timestamped_tag_beats_legacy_dated_tag_same_day(self, service):
        """A current date+time tag must rank above a legacy date-only tag from
        the same day. This is the real upgrade path: a field board installed
        from a legacy 'nightly-2026-06-19-<sha>' build must accept the first
        date+time release built later that same day. Regression: if the
        date-only stamp were padded on the left or compared at unequal width,
        the legacy build could read as newer and the board would never update.
        """
        assert service._is_newer(
            "nightly-20260619-031500-0a6a09c", "nightly-2026-06-19-23a8f85"
        ) is True

    def test_legacy_dated_tag_does_not_downgrade_newer_timestamped_build(self, service):
        """The inverse of the upgrade path: a legacy date-only tag must NOT be
        offered to a board already running a later date+time build from the same
        day. Regression: treating the padded date-only stamp as newer would
        downgrade the board.
        """
        assert service._is_newer(
            "nightly-2026-06-19-23a8f85", "nightly-20260619-031500-0a6a09c"
        ) is False

    def test_dpkg_version_string_breaks_nightly_comparison(self, service):
        """Documents WHY VERSION must hold the release tag, not the dpkg
        version: with the dpkg-style "2.0.0-nightly" as the current version,
        any nightly tag is reported as newer (the symptom of the original
        bug). This pins the rationale so the build-side fix is not reverted.
        """
        assert service._is_newer(
            "nightly-2026-06-17-f0e809a", "2.0.0-nightly"
        ) is True

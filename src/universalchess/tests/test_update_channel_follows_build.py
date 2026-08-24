"""Tests for the update channel following the installed build.

Root cause these guard
----------------------
``update-state.json`` stored the channel as a plain preference defaulting to
``"stable"``, and nothing ever seeded it from the build that was actually
installed. A board flashed or sideloaded with a nightly therefore reported
"Stable" in Settings and in the board's Updates menu, the update check filtered
every nightly release out, and the stable release still compared as newer (a
``nightly-...`` tag parses to an empty version tuple), so the board offered the
stable build and installed it with ``--allow-channel-switch`` -- migrating itself
off nightly without being asked.

The channel is now derived from the installed build, and the persisted
``channel`` field means something narrower: a *pending* switch the user selected
but has not installed yet. It is therefore stored only while it differs from the
installed build, and dissolves once the matching build lands. That distinction is
what these tests protect from both sides: deriving unconditionally would revert a
selection before the new channel's build could ever be downloaded (so switching
would silently stop working), while never deriving leaves the original bug.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import universalchess.services.update_service as us
from universalchess.services.update_service import UpdateChannel, UpdateService

# The VERSION a published nightly ships: scripts/build.sh writes RELEASE_TAG,
# which .github/workflows/nightly.yml sets to nightly-<stamp>-<sha>.
NIGHTLY_TAG = "nightly-2026-06-19-031500-abc1234"
# The dpkg version a nightly carries (control Version rewritten by the workflow),
# which is also the VERSION of a nightly built without RELEASE_TAG.
NIGHTLY_DPKG_VERSION = "2.0.0-nightly"
STABLE_VERSION = "2.0.0"


def _redirect_paths(tmp_path, monkeypatch):
    """Point every on-disk update path into tmp_path. Returns the state file."""
    state_file = tmp_path / "update-state.json"
    monkeypatch.setattr(us, "STATE_FILE", state_file)
    monkeypatch.setattr(us, "VERSION_FILE", tmp_path / "VERSION")
    monkeypatch.setattr(us, "PENDING_DEB_DIR", tmp_path / "pending-updates")
    return state_file


def _start_service(tmp_path, monkeypatch, installed_version, persisted_state=None):
    """Construct an UpdateService as a service start would, on a board running
    ``installed_version`` and (optionally) with ``persisted_state`` on disk.

    Constructing the service is what a restart does, so calling this twice over
    the same tmp_path models the board or web service coming back up -- including
    the restart the package postinst performs at the end of an install.

    subprocess.run is stubbed so get_current_version()'s dpkg-query fallback
    never touches the real system.
    """
    state_file = _redirect_paths(tmp_path, monkeypatch)
    (tmp_path / "VERSION").write_text(installed_version)
    if persisted_state is not None:
        state_file.write_text(json.dumps(persisted_state, indent=2))

    with patch.object(us.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=installed_version, stderr=""
        )
        return UpdateService()


def _persisted(tmp_path):
    """The state as written to disk, so the stored form is asserted directly."""
    return json.loads((tmp_path / "update-state.json").read_text())


def _stage_pending(service, tmp_path, name="universal-chess_2.0.0_all.deb"):
    """Record a downloaded .deb as pending, as a completed download would."""
    deb = tmp_path / "pending-updates" / name
    deb.parent.mkdir(parents=True, exist_ok=True)
    deb.write_bytes(b"deb")
    service._state.pending_deb = str(deb)
    service._save_state()
    return deb


@pytest.mark.parametrize(
    ("installed_version", "expected"),
    [
        # A published nightly: the reported bug -- this board read "stable".
        (NIGHTLY_TAG, UpdateChannel.NIGHTLY),
        # A nightly built without RELEASE_TAG carries the channel in its dpkg
        # version instead, so the tag format must not be the only thing checked.
        (NIGHTLY_DPKG_VERSION, UpdateChannel.NIGHTLY),
        (STABLE_VERSION, UpdateChannel.STABLE),
        # No VERSION file and no dpkg answer: nothing indicates nightly, and
        # stable is the channel that cannot pull unreviewed builds.
        ("unknown", UpdateChannel.STABLE),
    ],
)
def test_fresh_board_reports_the_installed_builds_channel(
    tmp_path, monkeypatch, installed_version, expected
):
    """With no prior state, the channel is the installed build's.

    Why this test exists: the channel used to default to "stable" regardless of
    the build, so a nightly board reported and followed the stable channel. Both
    readers are asserted because they resolve it separately -- get_channel() from
    this instance's state, get_status_dict() from a fresh on-disk snapshot for
    the other process -- and the board menu uses the first while the web Settings
    page uses the second.

    How a regression manifests: a nightly board reads "Stable" again (or, if the
    derivation is inverted, a stable board reads "Nightly" and starts pulling
    unreviewed builds). Storing an override here instead of leaving it null would
    pin the channel, so a later build in the other channel would not be followed.
    """
    service = _start_service(tmp_path, monkeypatch, installed_version)

    assert service.get_channel() == expected
    assert service.get_status_dict()["channel"] == expected.value
    assert _persisted(tmp_path)["channel"] is None


def test_selected_channel_survives_a_restart_before_its_build_is_installed(
    tmp_path, monkeypatch
):
    """A pending switch outlives a service restart.

    Why this test exists: the selection and the install are separate user
    actions, and either service may restart in between. If the channel were
    derived unconditionally, the selection would be reverted before the new
    channel's build could be found and downloaded, so switching channels would
    silently never work -- the failure mode that makes "derive it from the build"
    wrong on its own.

    How a regression manifests: a user selects Nightly on a stable board, the
    board restarts (or the next check runs in the other process), and the board
    is back on Stable with no explanation.
    """
    service = _start_service(tmp_path, monkeypatch, STABLE_VERSION)
    service.set_channel(UpdateChannel.NIGHTLY)
    assert _persisted(tmp_path)["channel"] == UpdateChannel.NIGHTLY.value

    restarted = _start_service(tmp_path, monkeypatch, STABLE_VERSION)

    assert restarted.get_channel() == UpdateChannel.NIGHTLY
    assert restarted.get_status_dict()["channel"] == UpdateChannel.NIGHTLY.value
    # The divergence from the installed build is what tells the root install
    # helper this update is a deliberate switch rather than a rollback.
    assert restarted._is_channel_switch() is True


@pytest.mark.parametrize(
    ("installed_before", "selected", "installed_after"),
    [
        (STABLE_VERSION, UpdateChannel.NIGHTLY, NIGHTLY_TAG),
        (NIGHTLY_TAG, UpdateChannel.STABLE, STABLE_VERSION),
    ],
)
def test_pending_switch_dissolves_once_its_build_is_installed(
    tmp_path, monkeypatch, installed_before, selected, installed_after
):
    """Installing the selected channel's build clears the override.

    Why this test exists: the override exists only to describe a switch that has
    not happened yet. Left behind after the install it would stop the board from
    following its own build -- a later sideloaded build in the other channel
    would be reported under the stale selection -- and it would keep
    ``--allow-channel-switch`` on every subsequent same-channel update, which
    disables the helper's rollback protection.

    How a regression manifests: the channel stays correct (the override and the
    build now agree, so no reader disagrees), and only the rollback guard
    silently stops applying -- invisible without this assertion.
    """
    service = _start_service(tmp_path, monkeypatch, installed_before)
    service.set_channel(selected)

    # The install replaces the build and the postinst restarts both services.
    restarted = _start_service(tmp_path, monkeypatch, installed_after)

    assert restarted.get_channel() == selected
    assert _persisted(tmp_path)["channel"] is None
    assert restarted._is_channel_switch() is False


def test_reselecting_the_running_channel_keeps_a_staged_update(tmp_path, monkeypatch):
    """Choosing the channel the board already runs is not a switch.

    Why this test exists: switching channels discards a build staged for the old
    channel, which is correct, but the discard used to happen on every channel
    write. Re-selecting the current channel would then throw away a downloaded
    update the user was about to install, forcing a full re-download on a board
    that may be on slow Wi-Fi.

    How a regression manifests: the staged .deb and the pending marker disappear
    after a no-op selection, and Settings drops from "Ready to install" back to
    "Download".
    """
    service = _start_service(tmp_path, monkeypatch, NIGHTLY_TAG)
    deb = _stage_pending(service, tmp_path)

    service.set_channel(UpdateChannel.NIGHTLY)

    assert deb.exists()
    assert service.has_pending_update() is True
    assert _persisted(tmp_path)["channel"] is None


def test_switching_channel_discards_the_old_channels_staged_update(
    tmp_path, monkeypatch
):
    """A real switch still discards what was staged for the old channel.

    Why this test exists: guards the other half of the conditional added above --
    the discard must remain for an actual change, or the board would install a
    stable build while claiming to have switched to nightly.

    How a regression manifests: the staged .deb survives the switch and the next
    install applies a build from the channel the user just left.
    """
    service = _start_service(tmp_path, monkeypatch, NIGHTLY_TAG)
    deb = _stage_pending(service, tmp_path)
    service._state.available_version = "2.0.0-nightly.2"
    service._state.available_release_tag = NIGHTLY_TAG
    service._save_state()

    service.set_channel(UpdateChannel.STABLE)

    assert not deb.exists()
    assert service.has_pending_update() is False
    stored = _persisted(tmp_path)
    assert stored["channel"] == UpdateChannel.STABLE.value
    assert stored["available_version"] is None
    assert stored["available_release_tag"] is None


class TestExistingStateFileMigration:
    """State files written before the channel followed the build.

    Such a file records a bare channel with no way to tell a chosen switch from
    the old default, so the two directions are resolved by what each one can mean:
    a persisted "nightly" cannot have come from the default and was therefore
    chosen, while a persisted "stable" on a nightly board is the bug itself. The
    resolution runs once -- keyed on the state version -- because applying it on
    every start would revert a nightly board's deliberate move to stable.
    """

    # A state file as written before this change: a bare channel, no version.
    def _legacy_state(self, channel):
        return {
            "channel": channel,
            "auto_update": False,
            "pending_deb": None,
            "last_check": "2026-06-19T03:15:00+00:00",
            "available_version": None,
            "available_release_tag": None,
            "current_version": None,
        }

    def test_legacy_stable_on_a_nightly_board_is_corrected(self, tmp_path, monkeypatch):
        """The reported bug on a board that already has a state file.

        Why this test exists: the fix must reach boards already in the field, not
        only freshly flashed ones -- those boards are the reported case. Their
        file says "stable" purely because that was the default, and the stale
        stable offer it accumulated belongs to a channel the board is not on.

        How a regression manifests: new boards read Nightly correctly while
        existing ones stay stuck on Stable, so the bug looks fixed in testing and
        persists on every board that was already running.
        """
        service = _start_service(
            tmp_path,
            monkeypatch,
            NIGHTLY_TAG,
            persisted_state={
                **self._legacy_state(UpdateChannel.STABLE.value),
                "available_version": STABLE_VERSION,
                "available_release_tag": f"v{STABLE_VERSION}",
            },
        )

        assert service.get_channel() == UpdateChannel.NIGHTLY
        stored = _persisted(tmp_path)
        assert stored["channel"] is None
        # The offer was computed for the channel the board is not on.
        assert stored["available_version"] is None
        assert stored["available_release_tag"] is None

    def test_legacy_nightly_on_a_stable_board_is_preserved(self, tmp_path, monkeypatch):
        """A switch chosen before the upgrade is not clobbered by it.

        Why this test exists: a stable board can only hold a persisted "nightly"
        because someone selected it, so treating the whole legacy field as
        derivable would discard a switch that is in flight -- silently, at the
        moment the user upgrades.

        How a regression manifests: the board reverts to Stable after the upgrade
        and the nightly the user was waiting for never arrives.
        """
        service = _start_service(
            tmp_path,
            monkeypatch,
            STABLE_VERSION,
            persisted_state=self._legacy_state(UpdateChannel.NIGHTLY.value),
        )

        assert service.get_channel() == UpdateChannel.NIGHTLY
        assert _persisted(tmp_path)["channel"] == UpdateChannel.NIGHTLY.value
        assert service._is_channel_switch() is True

    def test_migration_does_not_revert_a_later_move_off_nightly(
        self, tmp_path, monkeypatch
    ):
        """The correction applies once, not on every start.

        Why this test exists: this is the edge case the state version exists for.
        A nightly board that selects Stable holds exactly the combination the
        correction looks for ("stable" recorded, nightly installed). Re-running
        it on each start would revert that selection before the stable build
        could be installed, so the same nightly board could never leave the
        channel -- reintroducing the switching failure through the migration.

        How a regression manifests: the board flips back to Nightly on the next
        restart and the stable build is never offered, while every fresh-board
        test keeps passing.
        """
        service = _start_service(
            tmp_path,
            monkeypatch,
            NIGHTLY_TAG,
            persisted_state=self._legacy_state(UpdateChannel.STABLE.value),
        )
        # The correction has run; the user now genuinely selects stable.
        service.set_channel(UpdateChannel.STABLE)

        restarted = _start_service(tmp_path, monkeypatch, NIGHTLY_TAG)

        assert restarted.get_channel() == UpdateChannel.STABLE
        assert restarted._is_channel_switch() is True


def test_check_for_updates_filters_by_the_derived_channel(tmp_path, monkeypatch):
    """The derived channel drives release selection, not just the display.

    Why this test exists: reporting the right channel while still filtering by
    the old default would leave the bug's actual consequence in place -- a
    nightly board being offered, and silently migrated to, the stable release.
    Entering through check_for_updates keeps this tied to the behavior rather
    than to how the channel happens to be read internally.

    How a regression manifests: the Settings page reads "Nightly" and the board
    is offered v2.0.0 stable anyway, so the display looks fixed while the board
    still leaves the nightly channel on the next install.
    """
    service = _start_service(tmp_path, monkeypatch, NIGHTLY_TAG)

    releases = json.dumps(
        [
            {
                "tag_name": f"v{STABLE_VERSION}",
                "name": STABLE_VERSION,
                "prerelease": False,
                "published_at": "2026-06-20T00:00:00Z",
                "assets": [
                    {
                        "name": f"universal-chess_{STABLE_VERSION}_all.deb",
                        "browser_download_url": "https://example.invalid/stable.deb",
                        "size": 1,
                    }
                ],
            },
            {
                "tag_name": "nightly-2026-06-20-031500-def5678",
                "name": "nightly",
                "prerelease": True,
                "published_at": "2026-06-20T03:15:00Z",
                "assets": [
                    {
                        "name": "universal-chess_2.0.0-nightly_all.deb",
                        "browser_download_url": "https://example.invalid/nightly.deb",
                        "size": 1,
                    }
                ],
            },
        ]
    )

    with patch.object(us.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=releases, stderr="")
        release = service.check_for_updates()

    assert release is not None
    assert release.tag == "nightly-2026-06-20-031500-def5678"
    assert release.is_nightly is True

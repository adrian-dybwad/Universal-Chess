"""The status-bar indicator that says an engine install is still running.

Background / why these tests exist
----------------------------------
Backgrounding an install with BACK is only safe if the board keeps saying the
install is there. Without an indicator the build becomes invisible: nothing on
screen distinguishes "installing Reckless for the next hour" from idle, so the
user powers the board off, or starts the same install again, and loses the work.

Installs run in the web process, so the board learns about them two ways, and it
needs both.

*Pushes*, as ``engine_install_status`` board commands on the settings socket --
the channel that already carries shutdown and reboot -- so the icon appears and
clears promptly. These are fanned out through the engine manager's existing
progress listeners, which is what the widget was already built to consume.

*The persisted install state*, read when a widget is created. The status bar is
rebuilt from scratch on every screen change, which is exactly what backgrounding
an install does, so the widget that received the push is destroyed moments later.
Its replacement has no event to learn from and must ask. A push alone would leave
the indicator blank for the rest of an hour-long build; a file read alone would
leave it stale until the next screen change. Reading a file is also
self-correcting in a way a missed datagram is not -- a board restarted mid-install
picks up the truth immediately.
"""

import pytest

from universalchess.managers.engine_manager import EngineManager
from universalchess.services.engine_install_state import InstallStateStore

ENGINE = "reckless"
OTHER_ENGINE = "stockfish"


@pytest.fixture
def manager(tmp_path):
    """An engine manager rooted in the sandbox, never the production /opt."""
    return EngineManager(engines_dir=str(tmp_path / "engines"))


@pytest.fixture
def shared_store(tmp_path, monkeypatch):
    """Stand in for the install state the web process persists."""
    store = InstallStateStore(path=tmp_path / "engine_install_state.json")
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.INSTALL_STATE_STORE", store
    )
    return store


class TestActivityReportedFromTheWebProcess:
    """Pushed status reaches this process's listeners."""

    def test_reported_activity_reaches_the_listeners(self, manager):
        """A reported event is fanned out unchanged.

        Why: the install runs in the web process, which has no engine manager
        here, so nothing local would ever emit these. This is the entry point the
        settings-socket handler calls, and it must deliver the same
        (engine, status, message) shape the widget already understands rather than
        a second parallel notification path.

        How a regression manifests: the icon never appears or never clears for
        installs, which is all of them.
        """
        events = []
        manager.add_progress_listener(lambda *event: events.append(event))

        manager.notify_install_activity(ENGINE, "installing", "Building...")

        assert events == [(ENGINE, "installing", "Building...")]

    def test_a_listener_that_raises_does_not_break_the_others(self, manager):
        """One broken listener does not silence the rest.

        Why: these arrive on the socket listener thread, which also delivers
        settings changes and board commands. An exception escaping into it would
        take down far more than the icon.

        How a regression manifests: a render error in one widget stops every
        later listener from being told anything.
        """
        events = []
        manager.add_progress_listener(lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
        manager.add_progress_listener(lambda *event: events.append(event))

        manager.notify_install_activity(ENGINE, "installing", "Building...")

        assert len(events) == 1


class TestWhatIsRunningRightNow:
    """The question a freshly built status bar asks."""

    def test_an_install_running_in_the_web_process_is_reported(self, manager,
                                                               shared_store):
        """An active install in the shared state names its engine.

        Why: the web process records what it is installing in the shared state
        file, and that file is the only thing the board can consult about work it
        is not doing itself. Pushes cover the transitions; this covers a widget
        built in the middle of one.

        How a regression manifests: backgrounding an install rebuilds the status
        bar and the icon vanishes -- at the exact moment it becomes the only sign
        the install exists.
        """
        shared_store.start(OTHER_ENGINE, "Stockfish", estimated_seconds=60)

        assert manager.active_install_engine() == OTHER_ENGINE

    def test_a_finished_install_in_the_shared_state_is_not_reported(self, manager,
                                                                    shared_store):
        """A completed install in the shared state reports nothing running.

        Why: the state file outlives the install it describes -- it is kept so the
        web UI can show the last result. Reading presence rather than the active
        flag would pin the icon on permanently after the first install the board
        ever performs.

        How a regression manifests: the gear appears at boot and never leaves.
        """
        shared_store.start(OTHER_ENGINE, "Stockfish", estimated_seconds=60)
        shared_store.finish(success=True)

        assert manager.active_install_engine() is None

    def test_nothing_running_anywhere_reports_nothing(self, manager, shared_store):
        """With no install in flight the answer is None.

        Why: the null case. Everything about the indicator is driven by this
        answer, so a truthy default would show a permanent gear on an idle board.

        How a regression manifests: the icon is visible on a board that has never
        installed anything.
        """
        assert manager.active_install_engine() is None

    def test_the_state_is_re_read_rather_than_remembered(self, manager, shared_store):
        """Each question re-reads the file the other process is writing.

        Why: this process is not the writer. An answer cached from the first read
        would describe whatever was happening when the board booted, for as long
        as it stays up.

        How a regression manifests: the icon reflects an install that finished
        hours ago, or never appears because the file was empty at startup.
        """
        assert manager.active_install_engine() is None

        shared_store.start(ENGINE, "Reckless", estimated_seconds=60)

        assert manager.active_install_engine() == ENGINE


class TestTheIndicatorWidget:
    """The status-bar widget's own visibility rules."""

    @pytest.fixture
    def widget_for(self, monkeypatch):
        """Build an ``InstallStatusWidget`` bound to a stand-in engine manager."""
        from universalchess.epaper.install_status import InstallStatusWidget

        def build(engine_manager):
            monkeypatch.setattr(
                "universalchess.managers.engine_manager.get_engine_manager",
                lambda: engine_manager,
            )
            return InstallStatusWidget(0, 0, 16, lambda *args, **kwargs: None)

        return build

    def test_a_widget_built_during_an_install_starts_visible(self, manager,
                                                             monkeypatch, widget_for):
        """The icon is showing before any push arrives.

        Why: the status bar is rebuilt on every screen change, so the widget that
        received the push is destroyed the moment the user backgrounds the
        install. Its replacement has no event to learn from and must ask.

        How a regression manifests: the widget starts hidden, and because a build
        in progress sends no fresh start event, the indicator stays hidden for the
        rest of the install.
        """
        monkeypatch.setattr(manager, "active_install_engine", lambda: ENGINE)

        assert widget_for(manager).visible is True

    def test_a_widget_built_on_an_idle_board_starts_hidden(self, manager,
                                                           monkeypatch, widget_for):
        """With nothing installing the icon is absent.

        Why: the null case for the same lookup. The status bar is narrow and every
        widget in it competes for space, so an indicator that is always present
        costs the icons that mean something.

        How a regression manifests: a gear sits in the status bar of a board that
        is not installing anything.
        """
        monkeypatch.setattr(manager, "active_install_engine", lambda: None)

        assert widget_for(manager).visible is False

    def test_the_icon_goes_away_when_the_install_ends(self, manager, monkeypatch,
                                                      widget_for):
        """A terminal push hides an icon that started visible.

        Why: seeding at construction must not outrank later events. A widget that
        seeded itself visible and then ignored the completion would keep reporting
        an install that finished minutes ago.

        How a regression manifests: the gear survives the end of the install and
        only clears on the next screen change.
        """
        monkeypatch.setattr(manager, "active_install_engine", lambda: ENGINE)
        widget = widget_for(manager)

        manager.notify_install_activity(ENGINE, "completed", "Installation complete")

        assert widget.visible is False

    def test_the_icon_appears_on_a_push_for_an_idle_widget(self, manager,
                                                           monkeypatch, widget_for):
        """A widget that started hidden shows the icon when an install begins.

        Why: the ordinary case -- the board is sitting on a menu when someone
        starts an install from their phone. Nothing rebuilds the status bar, so
        the push is the only thing that can light the icon.

        How a regression manifests: installs started from the web are invisible on
        the board until something else redraws the screen.
        """
        monkeypatch.setattr(manager, "active_install_engine", lambda: None)
        widget = widget_for(manager)

        manager.notify_install_activity(ENGINE, "installing", "Building...")

        assert widget.visible is True

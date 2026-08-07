"""The board's engine install screens, now that the web process runs the builds.

Background / why these tests exist
----------------------------------
The board used to install engines itself, on its own ``EngineManager``, while the
web process installed on another. Two owners meant two truths: a build stopped
from the board preserved its tree but recorded no resume point, so the work could
be neither continued nor reclaimed from either surface, and both processes could
start an install at the same time with nothing to prevent it.

The web process now owns every install. These screens ask it to act, over the
existing sockets, and read progress from the persisted install state -- the same
record the web UI renders and the same one that survives a restart of either
process. That is what makes install, stop, resume and discard work identically
from the board and from a phone.

Three behaviours on the progress screen matter enough to pin down.

*BACK leaves the install running.* Waiting an hour in front of one screen is not a
reasonable price for having started a build. The status bar carries the fact that
it is still going, and the engine's own screen offers a way back to it -- without
which a backgrounded install could never be stopped.

*TICK opens the options, which is where Stop lives.* BACK no longer means stop, and
a build is expensive to lose to a stray press. A named row is also discoverable in
a way an undocumented key is not.

*Discard appears only once the install has stopped.* Deleting a tree that a
compiler still holds open races the build instead of reclaiming finished work.
"""

import logging
from types import SimpleNamespace

import pytest

from universalchess.menus.engine_manager_menu import (
    show_engine_install_progress,
    watch_engine_install_progress,
)
from universalchess.services.install_control import InstallActionResult

ENGINE = "reckless"
DISPLAY_NAME = "Reckless"
ESTIMATED_MINUTES = 60
REF = "v2.1.0"
BUILD_PERCENT = 65


class _Key:
    """The board key codes these screens care about."""

    BACK = "BACK"
    TICK = "TICK"
    PLAY = "PLAY"
    UP = "UP"


class _Splash:
    """Records every message a screen displays."""

    def __init__(self, *args, **kwargs):
        self.messages = [kwargs.get("message", "")]

    def set_message(self, message):
        self.messages.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


class _DisplayManager:
    def __init__(self):
        self.widgets = []

    def clear_widgets(self, addStatusBar=True):
        pass

    def update(self, *args, **kwargs):
        pass

    def add_widget(self, widget):
        self.widgets.append(widget)
        return None

    def remove_widget(self, widget):
        pass


class _Board:
    """A board whose key queue is scripted by the test."""

    SOUND_GENERAL = "general"
    Key = _Key

    def __init__(self, keys=()):
        self.display_manager = _DisplayManager()
        self.controller = SimpleNamespace(get_next_key=self._next_key)
        self._keys = list(keys)
        self.beeps = []

    def _next_key(self, timeout=0.0):
        return self._keys.pop(0) if self._keys else None

    def beep(self, sound, event_type=None):
        self.beeps.append((sound, event_type))

    @property
    def splash(self):
        return self.display_manager.widgets[0]


class _Control:
    """Stands in for the install-control client that talks to the web process."""

    def __init__(self, *, accepted=True, message="Installing Reckless"):
        self.calls = []
        self._result = InstallActionResult(accepted=accepted, message=message)

    def _record(self, action, **params):
        self.calls.append((action, params))
        return self._result

    def install(self, engine, ref=None):
        return self._record("install", engine=engine, ref=ref)

    def resume(self, engine):
        return self._record("resume", engine=engine)

    def stop(self):
        return self._record("stop")

    def discard(self, engine):
        return self._record("discard", engine=engine)

    def actions(self):
        return [action for action, _params in self.calls]


def _status(*, active=True, percent=BUILD_PERCENT, stage="building",
            message="crate 41 of 120", stopped=False, engine=ENGINE,
            result=None):
    """One reading of the persisted install state, as the board sees it."""
    return {
        "active": active,
        "installing": active,
        "engine": engine,
        "display_name": DISPLAY_NAME,
        "stage": stage,
        "message": message,
        "percent": percent,
        "stopped": stopped,
        "interrupted": False,
        "result": result,
        "eta_seconds": 180 if active else None,
    }


class _StateFile:
    """Replays a scripted sequence of install-state readings.

    The last reading repeats forever, so a test only has to describe the
    transition it cares about rather than every poll the loop performs.
    """

    def __init__(self, readings):
        self._readings = list(readings)
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if len(self._readings) > 1:
            return self._readings.pop(0)
        return self._readings[0]


class _ScriptedMenuManager:
    """Fake MenuManager driving run_menu_loop / show_menu deterministically.

    Mirrors the one in test_accounts_menu: ``run_menu_loop`` builds the entries
    once per scripted selection and dispatches it, while ``show_menu`` pops queued
    results so a prompt can be answered.
    """

    def __init__(self, selections=None, show_results=None):
        self.selections = list(selections or [])
        self.show_results = list(show_results or [])
        self.built = []
        self.shown = []
        self.show_initial_indexes = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0):
        self.built.append(build_entries())
        for selection in self.selections:
            handler_result = handle_selection(selection)
            if handler_result is not None:
                return handler_result
        return None

    def show_menu(self, entries, initial_index=0, on_index_change=None):
        self.shown.append(entries)
        self.show_initial_indexes.append(initial_index)
        if not self.show_results:
            # An unscripted menu is dismissed, so a test can pin one prompt
            # without having to answer every prompt that follows it.
            return _selection("BACK")
        return self.show_results.pop(0)

    def shown_keys(self, index=0):
        """The entry keys of the ``index``-th menu this manager was asked to show."""
        return [entry.key for entry in self.shown[index]]


@pytest.fixture(autouse=True)
def fast_splashless_loop(monkeypatch):
    """Run the poll loop at full speed against a recording splash screen.

    The loop sleeps half a second per turn to pace the e-paper refresh; left in
    place every test here would take seconds of wall clock for no added coverage.
    The real SplashScreen needs a display; the stand-in keeps every message it was
    given so the tests can assert what the user was told.
    """
    monkeypatch.setattr(
        "universalchess.menus.engine_manager_menu.time.sleep", lambda _seconds: None
    )
    monkeypatch.setattr(
        "universalchess.menus.engine_manager_menu.SplashScreen", _Splash
    )


@pytest.fixture
def logger():
    """A real logger writing nowhere, so log calls in the loop are exercised."""
    log = logging.getLogger("test_engine_install_stop_menu")
    log.addHandler(logging.NullHandler())
    return log


@pytest.fixture
def resume_store(tmp_path):
    """A resume-point store rooted in the sandbox, never the production /opt."""
    from universalchess.services.install_resume import ResumePointStore

    return ResumePointStore(build_root=tmp_path / "engine_build")


def _run(board, logger, *, control=None, read_status=None, menu_manager=None,
         resume_at_ref=None):
    return show_engine_install_progress(
        ENGINE, DISPLAY_NAME, ESTIMATED_MINUTES, board, logger,
        menu_manager=menu_manager or _ScriptedMenuManager(),
        control=control or _Control(),
        read_status=read_status or _StateFile([_status(active=False, result={"success": True})]),
        resume_at_ref=resume_at_ref,
    )


def _building_then(final):
    """Readings for a build that runs for two polls and then reaches ``final``."""
    return _StateFile([_status(), _status(), final])


def _selection(key):
    from universalchess.managers.menu import MenuSelection

    return MenuSelection.from_key(key)


def _stop_options(choose):
    return _ScriptedMenuManager(show_results=[_selection(choose)])


# ---------------------------------------------------------------------------
# Asking the web process to install
# ---------------------------------------------------------------------------


def test_opening_the_screen_requests_the_install(logger):
    """The screen asks the web process to install, naming the ref.

    Why: the board no longer builds anything itself. If the request is not sent,
    nothing installs at all and the screen watches a build that does not exist.
    The ref is the version chosen on the board, and the far end resolves and
    records it with the install state.

    How a regression manifests: no request is made, or it drops the ref and the
    catalog pin is built instead of the version the user picked.
    """
    control = _Control()

    _run(_Board(), logger, control=control, resume_at_ref=None)

    assert control.calls == [("install", {"engine": ENGINE, "ref": None})]


def test_resuming_asks_for_a_resume_rather_than_a_fresh_install(logger):
    """A resume is a different request, not an install with a ref.

    Why: only a resume reuses the preserved tree. An install request would
    re-clone, destroying exactly the work the user chose to continue, and the two
    are told apart by the action name alone.

    How a regression manifests: Resume takes the full build time again and the
    preserved tree is silently discarded.
    """
    control = _Control()

    _run(_Board(), logger, control=control, resume_at_ref=REF)

    assert control.calls == [("resume", {"engine": ENGINE})]


def test_a_refused_request_is_reported_and_nothing_is_watched(logger):
    """A rejection shows its reason instead of a progress bar.

    Why: the web refuses when another install is already running, and only it
    knows that. Watching anyway would show a progress screen fed by another
    engine's install, and BACK would appear to background a build the user never
    started.

    How a regression manifests: the refusal is swallowed, and the board shows
    progress for an install that was never dispatched.
    """
    control = _Control(accepted=False, message="Already installing Berserk")
    state = _StateFile([_status(engine="berserk")])
    board = _Board()

    assert _run(board, logger, control=control, read_status=state) is False

    assert state.reads == 0, "a refused request must not be watched"
    assert "Already installing Berserk" in board.splash.text


# ---------------------------------------------------------------------------
# Watching it, and the two ways out
# ---------------------------------------------------------------------------


def test_the_progress_screen_says_what_back_and_tick_do(logger):
    """The screen documents both controls it offers.

    Why: neither is guessable. BACK backing out of a screen normally abandons what
    it was doing, so a user who is not told otherwise will assume it kills the
    build and sit through the hour instead; and nothing at all hints that TICK
    opens a menu.

    How a regression manifests: a hint is dropped and its control becomes
    invisible -- users either wait out builds they could have backgrounded, or
    cannot find how to stop one.
    """
    board = _Board()

    _run(board, logger, read_status=_building_then(_status(active=False)))

    shown = board.splash.text
    assert "BACK" in shown
    assert "TICK" in shown


def test_the_screen_shows_the_percent_from_the_shared_state(logger):
    """Progress comes from the record the web UI shows too.

    Why: reading the shared state is what makes the two surfaces agree, and it is
    richer than what the board could produce alone -- a real percent rather than a
    truncated line of compiler output.

    How a regression manifests: the board shows a bare progress string, or a
    percent that disagrees with the web's for the same install.
    """
    board = _Board()

    _run(board, logger, read_status=_building_then(_status(active=False)))

    assert f"{BUILD_PERCENT}%" in board.splash.text


def test_pressing_back_leaves_the_install_running(logger):
    """BACK returns to the menu without touching the install.

    Why: this is the point of backgrounding. The screen is a view of the install,
    not the thing performing it, so leaving the view must not end the work.

    How a regression manifests: a stop is requested after a press that was only
    meant to close the screen, and an hour of compiling ends because the user
    wanted to look at something else.
    """
    control = _Control()
    board = _Board(keys=[_Key.BACK])

    assert _run(board, logger, control=control,
                read_status=_StateFile([_status()])) is False

    assert control.actions() == ["install"], "backgrounding must request nothing else"


def test_tick_opens_the_options_menu_and_stop_ends_the_install(logger):
    """Choosing Stop from the options menu asks the web process to stop.

    Why: with BACK now backgrounding, this menu is the only way to stop an install
    from the board.

    How a regression manifests: no stop is requested and a build the user chose to
    stop keeps running, with no remaining way to end it from the board.
    """
    control = _Control()
    board = _Board(keys=[_Key.TICK])
    state = _StateFile([_status(), _status(active=False, stopped=True)])

    _run(board, logger, control=control, read_status=state,
         menu_manager=_stop_options("stop"))

    assert control.actions() == ["install", "stop"]


def test_the_options_menu_does_not_offer_discard_while_the_build_runs(logger):
    """Discard is absent from the options of a running install.

    Why: discarding deletes the build tree, and a running build holds that tree
    open and is still writing to it. Offering it here would delete files from
    under a live compiler rather than reclaiming finished work.

    How a regression manifests: a Discard row appears next to Stop, and taking it
    races the build it is deleting.
    """
    board = _Board(keys=[_Key.TICK])
    menu_manager = _stop_options("stop")
    state = _StateFile([_status(), _status(active=False, stopped=True)])

    _run(board, logger, read_status=state, menu_manager=menu_manager)

    assert "stop" in menu_manager.shown_keys()
    assert "discard" not in menu_manager.shown_keys()


def test_backing_out_of_the_options_menu_leaves_the_install_running(logger):
    """Opening the options and choosing nothing changes nothing.

    Why: the options menu is reached with a single press, so it will be opened by
    accident. Leaving it must be free -- the null case for the menu, and the one
    that separates "looked at the options" from "asked for a stop".

    How a regression manifests: merely opening the menu stops the install, or the
    screen never returns to showing progress afterwards.
    """
    control = _Control()
    board = _Board(keys=[_Key.TICK])
    state = _building_then(_status(active=False, result={"success": True}))

    assert _run(board, logger, control=control, read_status=state,
                menu_manager=_stop_options("BACK")) is True
    assert control.actions() == ["install"]


def test_other_keys_do_nothing(logger):
    """Keys other than BACK and TICK are ignored.

    Why: the board's keys are close together and PLAY is pressed constantly during
    play. Treating any key as a control would background or stop builds by
    accident, and the user would have no idea which press did it.

    How a regression manifests: an unrelated press opens the options menu or ends
    the screen early.
    """
    control = _Control()
    menu_manager = _ScriptedMenuManager()
    board = _Board(keys=[_Key.PLAY, _Key.UP])
    state = _building_then(_status(active=False, result={"success": True}))

    assert _run(board, logger, control=control, read_status=state,
                menu_manager=menu_manager) is True
    assert control.actions() == ["install"]
    assert menu_manager.shown == [], "no key but TICK opens the options"


def test_an_install_that_finishes_reports_success(logger):
    """A completed install ends the screen with success.

    Why: the null case for all the input handling -- nobody touched the board and
    the build simply finished. The return value drives what the caller does next.

    How a regression manifests: a successful install reports failure, or the loop
    never notices that the shared state went inactive and watches forever.
    """
    board = _Board()
    state = _building_then(_status(active=False, result={"success": True}))

    assert _run(board, logger, read_status=state) is True
    assert "installed" in board.splash.messages[-1].lower()


def test_a_failed_install_reports_the_error_from_the_shared_state(logger):
    """A build that fails shows why, from the record the web wrote.

    Why: the error lives in the other process now. The board has no exception and
    no build log of its own, so if it does not read the message out of the shared
    state it has nothing to tell the user at all.

    How a regression manifests: a failed build shows a bare "failed" with no
    diagnostic, or is reported as a deliberate stop.
    """
    board = _Board()
    failed = _status(active=False, message="clang: not found",
                     result={"success": False, "error": "clang: not found"})

    assert _run(board, logger, read_status=_building_then(failed)) is False

    final = board.splash.messages[-1].lower()
    assert "clang" in final
    assert "stop" not in final


# ---------------------------------------------------------------------------
# What a stop leaves behind, and the chance to reclaim it
# ---------------------------------------------------------------------------


def test_another_engines_install_is_not_reported_as_this_one_s_ending(logger):
    """If the state has moved on to another engine, no outcome is claimed.

    Why: the shared state describes one install. If a second is started -- from
    the web, in the half-second between this one ending and the next poll -- the
    reading the loop exits on belongs to that install, not this one. Reading a
    result out of it would report a stranger's build: an active install carries no
    result, so it would surface as a failure with someone else's progress message
    attached.

    How a regression manifests: a successful install ends with "Install failed"
    and a message about an engine the user was not installing.
    """
    board = _Board()
    state = _StateFile([_status(), _status(engine="berserk", message="cloning")])

    assert _run(board, logger, read_status=state) is False

    final = board.splash.messages[-1].lower()
    assert "failed" not in final
    assert "cloning" not in final


def test_a_stopped_install_is_not_reported_as_a_failure(logger):
    """Stopping shows a stopped message, not an install error.

    Why: a stop and a failure are both "not active" in the shared state and are
    told apart only by the stopped flag. Falling into the failure branch would
    tell the user their deliberate stop went wrong.

    How a regression manifests: the final message says failed after the user chose
    to stop.
    """
    board = _Board(keys=[_Key.TICK])
    state = _StateFile([_status(), _status(active=False, stopped=True)])

    _run(board, logger, read_status=state, menu_manager=_ScriptedMenuManager(
        show_results=[_selection("stop"), _selection("keep")]
    ))

    final = board.splash.messages[-1].lower()
    assert "failed" not in final
    assert "stop" in final


def test_after_stopping_the_screen_offers_to_discard(logger):
    """The stopped screen asks whether to keep the work, defaulting to keeping it.

    Why: the moment a build stops is when the user knows whether they want its
    tree back, and it is the only point at which they are already looking at how
    far it got. Making them navigate back into the engine to reclaim the disk
    means most never will.

    Keep is the focused row because the menu is itself the confirmation: discard
    cannot be undone, so the destructive row must be the one that takes a
    deliberate move to reach, exactly as the detail screen's prompt defaults to
    Cancel.

    How a regression manifests: no menu appears after a stop, or discard sits
    under the cursor where a reflex press lands on it.
    """
    control = _Control()
    board = _Board(keys=[_Key.TICK])
    menu_manager = _ScriptedMenuManager(
        show_results=[_selection("stop"), _selection("keep")]
    )
    state = _StateFile([_status(), _status(active=False, stopped=True)])

    _run(board, logger, control=control, read_status=state,
         menu_manager=menu_manager)

    assert menu_manager.shown_keys(1) == ["keep", "discard"]
    assert menu_manager.show_initial_indexes[1] == 0, "the safe row must be focused"
    assert "discard" not in control.actions(), "keeping must preserve the work"


def test_discarding_from_the_stopped_screen_asks_the_web_to_remove_it(logger):
    """Choosing Discard there reclaims the tree through the same owner.

    Why: the resume point and the tree are written by the web process, so the
    board asks it to remove them rather than deleting files behind its back --
    which is how the two surfaces stopped agreeing in the first place.

    How a regression manifests: the board deletes the tree locally, or the row
    does nothing and the engine still reports a paused install.
    """
    control = _Control()
    board = _Board(keys=[_Key.TICK])
    menu_manager = _ScriptedMenuManager(
        show_results=[_selection("stop"), _selection("discard")]
    )
    state = _StateFile([_status(), _status(active=False, stopped=True)])

    _run(board, logger, control=control, read_status=state,
         menu_manager=menu_manager)

    assert control.calls[-1] == ("discard", {"engine": ENGINE})


def test_a_completed_install_is_not_offered_a_discard(logger):
    """Finishing normally shows no keep-or-discard menu.

    Why: the menu belongs to a stop. An install that succeeded has no preserved
    tree to reason about, and interrupting a success with a question about
    throwing work away is both confusing and a way to lose a fresh install.

    How a regression manifests: the menu appears whenever the screen closes, so
    every successful install ends with a prompt about discarding.
    """
    board = _Board()
    menu_manager = _ScriptedMenuManager()
    state = _building_then(_status(active=False, result={"success": True}))

    _run(board, logger, read_status=state, menu_manager=menu_manager)

    assert menu_manager.shown == []


# ---------------------------------------------------------------------------
# Returning to an install that was left running
# ---------------------------------------------------------------------------


def test_watching_a_running_install_requests_nothing(logger):
    """Reopening a backgrounded install does not dispatch another one.

    Why: the install is already running in the other process. Asking again would
    be refused as a concurrent install, and the screen would report that refusal
    over a build that is going perfectly well.

    How a regression manifests: returning to a backgrounded install reports
    "Already installing" and the user believes it broke.
    """
    control = _Control()
    board = _Board()
    state = _building_then(_status(active=False, result={"success": True}))

    watch_engine_install_progress(
        ENGINE, DISPLAY_NAME, ESTIMATED_MINUTES, board, logger,
        menu_manager=_ScriptedMenuManager(), control=control, read_status=state,
    )

    assert control.calls == []


def test_watching_still_offers_the_stop_option(logger):
    """A reopened install can be stopped like a freshly started one.

    Why: this is why the route back exists. Backgrounding must not be a one-way
    door -- if the reopened screen had no options menu, an install left running
    could never be ended from the board.

    How a regression manifests: the reopened screen is read-only and the only way
    to stop a backgrounded install is the web UI or a reboot.
    """
    control = _Control()
    board = _Board(keys=[_Key.TICK])
    state = _StateFile([_status(), _status(active=False, stopped=True)])
    menu_manager = _ScriptedMenuManager(
        show_results=[_selection("stop"), _selection("keep")]
    )

    watch_engine_install_progress(
        ENGINE, DISPLAY_NAME, ESTIMATED_MINUTES, board, logger,
        menu_manager=menu_manager, control=control, read_status=state,
    )

    assert control.actions() == ["stop"]


# ---------------------------------------------------------------------------
# The engine detail screen
# ---------------------------------------------------------------------------


def _pause(store, engine=ENGINE, percent=61, ref=REF):
    from universalchess.services.install_resume import ResumePoint

    store.write(ResumePoint(
        engine=engine, ref=ref, stage="building", message="Building",
        percent=percent, stopped_at=1_700_000_000.0, reason="stopped",
    ))


def _engine_info():
    return {
        "name": ENGINE,
        "display_name": DISPLAY_NAME,
        "summary": "Rust engine",
        "description": "A strong Rust engine",
        "estimated_install_minutes": ESTIMATED_MINUTES,
        "can_uninstall": True,
        "installed": False,
    }


@pytest.fixture
def local_engine_manager(monkeypatch):
    """An engine manager for the queries the detail screen still makes locally.

    Whether an engine is installed, and uninstalling it, remain board-local: they
    read and write the engines directory directly and involve no build, no
    progress and no shared state.
    """
    manager = SimpleNamespace(
        is_installed=lambda _name: False,
        uninstall_engine=lambda _name: True,
    )
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.get_engine_manager", lambda: manager
    )
    return manager


def _detail(menu_manager, board, logger, resume_store, *, control=None,
            read_status=None, install_progress=None, watch_progress=None):
    from universalchess.menus.engine_manager_menu import handle_engine_detail_menu

    return handle_engine_detail_menu(
        _engine_info(), menu_manager, board, logger,
        install_progress or (lambda *args, **kwargs: True),
        resume_store=resume_store,
        control=control or _Control(),
        read_status=read_status or (lambda: _status(active=False)),
        watch_install_progress=watch_progress or (lambda *args, **kwargs: True),
    )


def _entry_keys(menu_manager):
    return [entry.key for entry in menu_manager.built[0]]


def test_a_paused_engine_offers_resume_and_discard(logger, resume_store,
                                                   local_engine_manager):
    """A paused engine's detail screen offers to continue or throw away.

    Why: the board is the only interface some users have, and a preserved build
    tree it cannot act on is disk it can never reclaim. Plain Install is
    deliberately absent: it would start from scratch and silently destroy the
    preserved work, which is what Discard is for saying out loud.

    How a regression manifests: the screen still shows only Install, so the
    paused tree can be neither continued nor removed from the board.
    """
    _pause(resume_store, percent=61)
    menu_manager = _ScriptedMenuManager()

    _detail(menu_manager, _Board(), logger, resume_store)

    keys = _entry_keys(menu_manager)
    assert "resume" in keys
    assert "discard" in keys
    assert "install" not in keys


def test_the_resume_entry_says_how_far_the_install_got(logger, resume_store,
                                                       local_engine_manager):
    """The Resume row reports the paused percent.

    Why: it is the user's only basis for choosing between resuming and
    discarding. How a regression manifests: the row reads "Resume install" with
    no indication of whether an hour or a minute of work is at stake.
    """
    _pause(resume_store, percent=61)
    menu_manager = _ScriptedMenuManager()

    _detail(menu_manager, _Board(), logger, resume_store)

    resume_row = next(e for e in menu_manager.built[0] if e.key == "resume")
    assert "61%" in resume_row.label


def test_an_engine_with_no_paused_install_offers_a_plain_install(
    logger, resume_store, local_engine_manager
):
    """Without a resume point the screen is unchanged.

    Why: the paused controls are additional, not a replacement. This is the null
    case for the new lookup. How a regression manifests: every uninstalled engine
    offers Resume for a tree that does not exist.
    """
    menu_manager = _ScriptedMenuManager()

    _detail(menu_manager, _Board(), logger, resume_store)

    keys = _entry_keys(menu_manager)
    assert "install" in keys
    assert "resume" not in keys
    assert "discard" not in keys


def test_discard_is_confirmed_before_the_tree_is_removed(logger, resume_store,
                                                         local_engine_manager):
    """Choosing Discard asks first, and a refusal keeps the work.

    Why: discard destroys a build tree that may represent an hour of compiling and
    cannot be undone, and it sits next to Resume on a four-button device. The
    confirmation defaults to Cancel for the same reason account deletion does.

    How a regression manifests: no prompt is shown, or declining still deletes --
    either way a misclick costs the user the work they meant to continue.
    """
    _pause(resume_store)
    control = _Control()
    menu_manager = _ScriptedMenuManager(
        selections=[_selection("discard")], show_results=[_selection("Cancel")]
    )

    _detail(menu_manager, _Board(), logger, resume_store, control=control)

    assert len(menu_manager.shown) == 1, "discard must prompt before deleting"
    assert control.calls == []


def test_a_confirmed_discard_asks_the_web_to_remove_the_paused_install(
    logger, resume_store, local_engine_manager
):
    """Confirming Discard sends the request to the process that owns the tree.

    Why: the web writes the resume points and runs the removal, and routing every
    destructive action through one owner is what keeps the two surfaces agreeing
    about what exists.

    How a regression manifests: the board deletes the tree itself, so the web's
    guard against discarding a tree that is still being built no longer applies.
    """
    _pause(resume_store)
    control = _Control()
    menu_manager = _ScriptedMenuManager(
        selections=[_selection("discard")], show_results=[_selection("Discard")]
    )

    _detail(menu_manager, _Board(), logger, resume_store, control=control)

    assert control.calls == [("discard", {"engine": ENGINE})]


def test_resuming_from_the_board_opens_the_progress_screen_at_the_recorded_ref(
    logger, resume_store, local_engine_manager
):
    """Resume passes the paused install's ref to the progress screen.

    Why: the ref is what lets the preserved tree be reused instead of re-cloned,
    and it is what tells the screen to send a resume request rather than an
    install one.

    How a regression manifests: the recorded ref is None and the board's Resume
    silently rebuilds from scratch.
    """
    _pause(resume_store, ref=REF)
    started = {}
    menu_manager = _ScriptedMenuManager(selections=[_selection("resume")])

    def install_progress(engine_name, display_name, minutes, *args, **kwargs):
        started.update(engine=engine_name, **kwargs)
        return True

    _detail(menu_manager, _Board(), logger, resume_store,
            install_progress=install_progress)

    assert started["engine"] == ENGINE
    assert started["resume_at_ref"] == REF


def test_a_backgrounded_install_can_be_reopened_from_its_engine(
    logger, resume_store, local_engine_manager
):
    """An engine that is installing offers a way back to its progress screen.

    Why: BACK leaves the install running, and the options menu that holds Stop
    lives on the progress screen. Without a route back, backgrounding an install
    would make it unstoppable from the board -- the feature that lets the user
    walk away would take away the one that lets them change their mind.

    Install is deliberately absent: the web refuses a second concurrent install,
    so that row could only ever report a failure for a build that is running
    perfectly well.

    How a regression manifests: the screen offers Install for an engine already
    building, and the running install can only be ended from the web UI.
    """
    menu_manager = _ScriptedMenuManager()

    _detail(menu_manager, _Board(), logger, resume_store,
            read_status=lambda: _status(active=True))

    keys = _entry_keys(menu_manager)
    assert "view" in keys
    assert "install" not in keys


def test_another_engines_install_does_not_take_over_this_screen(
    logger, resume_store, local_engine_manager
):
    """A different engine's install leaves this engine's options alone.

    Why: the shared state describes one install, and it may not be this engine's.
    Reading "something is installing" as "this is installing" would offer to view
    Berserk's build from Reckless's screen, and hide Reckless's own Install row
    for the duration.

    How a regression manifests: starting any install makes every other engine's
    screen show View install progress.
    """
    menu_manager = _ScriptedMenuManager()

    _detail(menu_manager, _Board(), logger, resume_store,
            read_status=lambda: _status(active=True, engine="berserk"))

    keys = _entry_keys(menu_manager)
    assert "install" in keys
    assert "view" not in keys


def test_reopening_a_backgrounded_install_watches_instead_of_installing(
    logger, resume_store, local_engine_manager
):
    """Choosing it watches the running install instead of dispatching a new one.

    Why: the two look the same on screen and are entirely different underneath.
    Dispatching again is refused by the web as a concurrent install, so the screen
    would announce that the healthy install running behind it had failed.

    How a regression manifests: the install callback is used for the reopen, and
    viewing progress reports a failure for a build that is still going.
    """
    watched = []
    started = []
    menu_manager = _ScriptedMenuManager(selections=[_selection("view")])

    _detail(menu_manager, _Board(), logger, resume_store,
            read_status=lambda: _status(active=True),
            install_progress=lambda *args, **kwargs: started.append(args) or True,
            watch_progress=lambda *args, **kwargs: watched.append(args) or True)

    assert started == []
    assert len(watched) == 1
    assert ENGINE in watched[0]

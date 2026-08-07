"""Engine manager menu helpers.

The board does not install engines. The web process owns every install -- it runs
the build, holds the persisted install state, and writes the resume points -- and
these screens ask it to act and render what it reports. That is what lets an
install started on the board be stopped from a phone, and one started from a phone
be stopped on the board.

The progress screen is the board's one long-blocking view: a source build can run
for an hour. It polls the key queue while it waits, the same way the shutdown
countdown does, and offers two ways out. BACK leaves the install running, because
waiting an hour in front of one screen is not a reasonable price for having
started a build; the status bar carries it from there and the engine's own screen
offers a way back. TICK opens the options, which is where Stop lives -- a build is
expensive to lose to a stray press, and a named row is discoverable in a way an
undocumented key is not.

Discard is offered once an install has stopped, and only then: deleting a tree a
compiler still holds open races the build instead of reclaiming finished work.
"""

import time
from concurrent.futures import TimeoutError as RenderTimeoutError
from typing import Callable, Optional, List, Dict

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.epaper import SplashScreen
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.services import install_resume
from universalchess.services.install_control import get_install_control

# Poll interval for the install progress screen. Paces the e-paper refresh and
# bounds how long a key press waits to be noticed.
_PROGRESS_POLL_SECONDS = 0.5

# Progress text is truncated to what the splash can show on one line.
_PROGRESS_LINE_CHARS = 28
_ERROR_LINE_CHARS = 30

# How long a message the user must read stays on screen before the menu returns.
_MESSAGE_DWELL_SECONDS = 3

# How long to wait for a splash to reach the panel before carrying on regardless.
_SPLASH_RENDER_TIMEOUT_SECONDS = 2.0

# Resume points for paused installs, read by the engine detail screen. Bound to
# the real engine build directory; injectable so tests stay out of /opt.
_RESUME_STORE = None


def _wait_for_splash(promise, log, what: str) -> None:
    """Wait briefly for a splash to reach the panel, then continue regardless.

    The wait exists only so the user sees the screen before the work behind it
    starts; the work itself does not depend on the render. A slow, cancelled, or
    failed panel update is therefore logged and not raised -- aborting an install
    or uninstall the user asked for because a display refresh stalled would be
    the worse outcome. ``promise`` is None on displays that render synchronously.
    """
    if promise is None:
        return
    try:
        promise.result(timeout=_SPLASH_RENDER_TIMEOUT_SECONDS)
    except RenderTimeoutError:
        log.debug(
            f"[EngineManager] {what} splash not rendered within "
            f"{_SPLASH_RENDER_TIMEOUT_SECONDS}s; continuing"
        )
    except Exception as e:
        log.warning(f"[EngineManager] {what} splash failed to render: {e}")


def _resume_store():
    """Return the shared resume-point store, built on first use.

    Deferred because importing the engine manager at module import time would pull
    the whole install stack into every menu import.
    """
    global _RESUME_STORE
    if _RESUME_STORE is None:
        from universalchess.managers.engine_manager import BUILD_TMP

        _RESUME_STORE = install_resume.ResumePointStore(build_root=BUILD_TMP)
    return _RESUME_STORE


def read_install_status() -> dict:
    """Read the install state the web process maintains.

    Re-read from disk each time: this process is not the writer, so a cached
    answer would freeze the board's progress at whatever the file said when the
    screen opened.
    """
    from universalchess.services.engine_install_state import STORE

    return STORE.observed_status_dict()


def _show_splash(board, log, message: str, what: str):
    """Take over the screen with a message, returning the splash to update."""
    board.display_manager.clear_widgets(addStatusBar=False)
    splash = SplashScreen(
        board.display_manager.update,
        message=message,
        leave_room_for_status_bar=False,
    )
    _wait_for_splash(board.display_manager.add_widget(splash), log, what)
    return splash


def show_engine_install_progress(
    engine_name: str,
    display_name: str,
    estimated_minutes: int,
    board,
    log,
    *,
    menu_manager,
    control=None,
    read_status: Optional[Callable[[], dict]] = None,
    resume_at_ref: Optional[str] = None,
) -> bool:
    """Ask the web process to install an engine, then watch it happen.

    A resume is sent as its own request rather than as an install carrying a ref:
    only a resume reuses the preserved build tree, and the ref comes from the
    engine's resume point at the far end.

    Args:
        engine_name: Engine to install.
        display_name: Name to show on screen.
        estimated_minutes: Rough build time, shown before progress starts.
        board: Board module.
        log: Logger instance.
        menu_manager: Used for the options and keep-or-discard menus.
        control: Install-control client; defaults to the shared one.
        read_status: Reads the shared install state; defaults to the real file.
        resume_at_ref: Set when continuing a paused install, in which case a
            resume is requested instead of a fresh install.

    Returns:
        True only if the install completed successfully while this screen was
        open. False if it failed, was stopped, was refused, or was left running.
    """
    control = control if control is not None else get_install_control()
    read_status = read_status if read_status is not None else read_install_status

    if resume_at_ref is not None:
        log.info(f"[EngineManager] Requesting resume of {engine_name} at {resume_at_ref!r}")
        result = control.resume(engine_name)
    else:
        log.info(f"[EngineManager] Requesting install of {engine_name} "
                 f"(est. {estimated_minutes} min)")
        result = control.install(engine_name)

    if not result.accepted:
        # Refused by the owner of the install state -- another install running, an
        # unknown engine, a web service that is down. Watching anyway would show
        # progress for a build that was never dispatched.
        log.warning(f"[EngineManager] {engine_name} install refused: {result.message}")
        board.beep(board.SOUND_GENERAL, event_type="error")
        _show_splash(board, log, f"Cannot install\n{display_name}\n\n{result.message}",
                     f"{display_name} refusal")
        time.sleep(_MESSAGE_DWELL_SECONDS)
        return False

    return _watch_install(
        engine_name, display_name, estimated_minutes, board, log,
        menu_manager=menu_manager, control=control, read_status=read_status,
    )


def watch_engine_install_progress(
    engine_name: str,
    display_name: str,
    estimated_minutes: int,
    board,
    log,
    *,
    menu_manager,
    control=None,
    read_status: Optional[Callable[[], dict]] = None,
) -> bool:
    """Reopen the progress screen for an install that is already running.

    The counterpart to leaving one running with BACK. Requests nothing: the
    install is in flight, and asking again would be refused as a concurrent
    install and reported over a build that is going perfectly well.
    """
    return _watch_install(
        engine_name, display_name, estimated_minutes, board, log,
        menu_manager=menu_manager,
        control=control if control is not None else get_install_control(),
        read_status=read_status if read_status is not None else read_install_status,
    )


def _watch_install(engine_name, display_name, estimated_minutes, board, log,
                   *, menu_manager, control, read_status) -> bool:
    """Render the shared install state until the install ends or the user leaves."""
    progress_splash = _show_splash(
        board, log,
        f"Installing\n{display_name}\n\nMay take ~{estimated_minutes} min\n"
        f"BACK leave running\nTICK options",
        f"{display_name} install",
    )

    last_rendered = None
    stop_requested = False
    status = read_status()
    while status["active"] and status["engine"] == engine_name:
        rendered = (status["percent"], status["message"])
        if rendered != last_rendered and not stop_requested:
            last_rendered = rendered
            message = (status["message"] or "Working...")[:_PROGRESS_LINE_CHARS]
            progress_splash.set_message(
                f"Installing\n{display_name}...\n\n{status['percent']}%  {message}\n\n"
                f"BACK leave running\nTICK options"
            )

        key = board.controller.get_next_key(timeout=0.0)
        if key == board.Key.BACK:
            log.info(f"[EngineManager] Leaving the {engine_name} install running")
            return False
        # Only the first stop does anything: it is cooperative and the build takes
        # a moment to wind down, and asking twice would mean nothing different.
        if key == board.Key.TICK and not stop_requested:
            stop_requested = _offer_stop(
                engine_name, display_name, board, log,
                menu_manager=menu_manager, control=control, splash=progress_splash,
            )

        time.sleep(_PROGRESS_POLL_SECONDS)
        status = read_status()

    return _report_ending(
        engine_name, display_name, status, board, log,
        menu_manager=menu_manager, control=control, splash=progress_splash,
    )


def _offer_stop(engine_name, display_name, board, log, *, menu_manager, control,
                splash) -> bool:
    """Show the running-install options and act on the choice. True if stopped.

    Discard is deliberately absent: the build holds its tree open and is still
    writing to it, so deleting it here would race the compiler rather than
    reclaim finished work. It is offered once the install has actually stopped.
    """
    entries = [
        IconMenuEntry(key="stop", label=f"Stop {display_name} install",
                      icon_name="cancel", enabled=True, selectable=True,
                      height_ratio=1.0, layout="horizontal", font_size=14),
        IconMenuEntry(key="keep", label="Keep installing", icon_name="undo",
                      enabled=True, selectable=True, height_ratio=1.0,
                      layout="horizontal", font_size=14),
    ]
    choice = menu_manager.show_menu(entries, initial_index=0)
    if getattr(choice, "key", choice) != "stop":
        return False

    log.info(f"[EngineManager] Stop requested for the {engine_name} install")
    board.beep(board.SOUND_GENERAL)
    splash.set_message(f"Stopping\n{display_name}...")
    result = control.stop()
    if not result.accepted:
        log.warning(f"[EngineManager] Stop refused: {result.message}")
        splash.set_message(f"Could not stop\n{display_name}\n\n{result.message}")
    return result.accepted


def _report_ending(engine_name, display_name, status, board, log, *, menu_manager,
                   control, splash) -> bool:
    """Say how the install ended, and offer to reclaim the tree if it stopped."""
    if status["engine"] != engine_name:
        # The shared state has moved on to another install, started while this one
        # was ending. Its record says nothing about how this install finished, and
        # an active install carries no result, so reading one out would report a
        # stranger's build as this one's failure.
        log.info(f"[EngineManager] {engine_name} install ended; the board is now "
                 f"showing {status['engine']!r}")
        return False

    if status["stopped"]:
        # A stop is not a failure. Reporting it through the error branch would tell
        # the user their own choice went wrong, and show whatever error text the
        # last failed install happened to leave behind.
        log.info(f"[EngineManager] Installation of {engine_name} stopped by the user")
        board.beep(board.SOUND_GENERAL)
        splash.set_message(f"{display_name}\ninstall stopped\n\nat {status['percent']}%")
        _offer_discard(engine_name, display_name, board, log,
                       menu_manager=menu_manager, control=control, splash=splash)
        return False

    succeeded = bool((status["result"] or {}).get("success"))
    if succeeded:
        board.beep(board.SOUND_GENERAL)
        splash.set_message(f"{display_name}\ninstalled!")
        time.sleep(1.5)
        return True

    error = (status["result"] or {}).get("error") or status["message"] or "Unknown error"
    log.error(f"[EngineManager] Installation of {engine_name} failed: {error}")
    board.beep(board.SOUND_GENERAL, event_type="error")
    splash.set_message(f"Install failed\n\n{error[:_ERROR_LINE_CHARS]}")
    time.sleep(_MESSAGE_DWELL_SECONDS)
    return False


def _offer_discard(engine_name, display_name, board, log, *, menu_manager, control,
                   splash) -> None:
    """Offer to reclaim the stopped install's tree, defaulting to keeping it.

    Offered here because this is the moment the user knows whether they want the
    work back, and the only point at which they are already looking at how far it
    got; making them navigate back into the engine to reclaim the disk means most
    never will.

    The menu is itself the confirmation, with Keep focused: discard cannot be
    undone, so the destructive row is the one that takes a deliberate move to
    reach, exactly as the detail screen's prompt defaults to Cancel.
    """
    entries = [
        IconMenuEntry(key="keep", label="Keep it (resume later)", icon_name="undo",
                      enabled=True, selectable=True, height_ratio=1.0,
                      layout="horizontal", font_size=14),
        IconMenuEntry(key="discard", label=f"Discard {display_name} build",
                      icon_name="cancel", enabled=True, selectable=True,
                      height_ratio=1.0, layout="horizontal", font_size=14),
    ]
    choice = menu_manager.show_menu(entries, initial_index=0)
    if getattr(choice, "key", choice) != "discard":
        return

    log.info(f"[EngineManager] Discarding the stopped {engine_name} install")
    result = control.discard(engine_name)
    splash.set_message(
        f"{display_name}\nbuild discarded" if result.accepted
        else f"Could not discard\n\n{result.message}"
    )
    time.sleep(_MESSAGE_DWELL_SECONDS if not result.accepted else 1.5)


def confirm_discard_install(menu_manager, display_name: str = "") -> bool:
    """Show a Discard/Cancel confirmation and return True only if Discard is chosen.

    Discarding deletes a build tree that may hold an hour of compiling and cannot
    be undone, and its row sits next to Resume on a four-button device. The
    highlight defaults to Cancel so a stray confirmation press cannot destroy the
    work the user meant to continue; any non-Discard outcome is a refusal.
    """
    prompt = f"Discard paused\n{display_name} install?" if display_name else "Discard paused\ninstall?"
    entries = [
        IconMenuEntry(key="prompt", label=prompt, icon_name="cancel", enabled=True, selectable=False, font_size=12),
        IconMenuEntry(key="Discard", label="Discard", icon_name="cancel", enabled=True),
        IconMenuEntry(key="Cancel", label="Cancel", icon_name="undo", enabled=True),
    ]
    result = menu_manager.show_menu(entries, initial_index=2)
    key = getattr(result, "key", result)
    return key == "Discard"


def handle_engine_detail_menu(
    engine_info: dict,
    menu_manager,
    board,
    log,
    show_install_progress: Callable,
    resume_store=None,
    control=None,
    read_status: Optional[Callable[[], dict]] = None,
    watch_install_progress: Optional[Callable] = None,
) -> Optional[MenuSelection]:
    """Handle engine detail submenu.

    Shows the engine description and its install/uninstall option; the option to
    resume or discard a paused install; or, while this engine is installing, the
    way back to its progress screen.

    Args:
        engine_info: Dict with engine info from get_engine_list()
        menu_manager: Menu manager instance
        board: Board module
        log: Logger instance
        show_install_progress: Callback that requests an install and watches it
        resume_store: Source of paused-install records. Defaults to the shared
            store over the real build directory; injectable for tests.
        control: Install-control client; defaults to the shared one.
        read_status: Reads the shared install state; defaults to the real file.
        watch_install_progress: Callback that watches an install already running.

    Returns:
        MenuSelection if break, None otherwise
    """
    from universalchess.managers.engine_manager import get_engine_manager

    engine_manager = get_engine_manager()
    engine_name = engine_info["name"]
    display_name = engine_info["display_name"]
    resume_store = resume_store if resume_store is not None else _resume_store()
    control = control if control is not None else get_install_control()
    read_status = read_status if read_status is not None else read_install_status
    watch_progress = (watch_install_progress if watch_install_progress is not None
                      else watch_engine_install_progress)

    def is_installing_this_engine() -> bool:
        """Whether the one install slot is currently held by this engine.

        Checked per engine rather than as "something is installing": the shared
        state describes a single install, and another engine's build must not hide
        this one's Install row or offer to view a screen about something else.
        """
        status = read_status()
        return bool(status["active"]) and status["engine"] == engine_name

    def screen_arguments():
        return dict(menu_manager=menu_manager, control=control, read_status=read_status)

    def build_entries():
        entries = []
        is_installed = engine_manager.is_installed(engine_name)
        can_uninstall = engine_info.get("can_uninstall", True)
        installing = not is_installed and is_installing_this_engine()
        # Read on every rebuild: resuming or discarding changes it, and the screen
        # is redrawn after each.
        resume_point = None if is_installed or installing else resume_store.read(engine_name)

        entries.append(
            IconMenuEntry(
                key="title",
                label=f"{engine_info['display_name']}\n{engine_info['summary']}",
                icon_name="engine",
                enabled=True,
                selectable=False,
                height_ratio=2.8,
                layout="horizontal",
                font_size=14,
                bold=True,
                description=engine_info["description"],
                description_font_size=11,
            )
        )

        est_minutes = engine_info.get("estimated_install_minutes", 5)

        if is_installed:
            if can_uninstall:
                entries.append(
                    IconMenuEntry(
                        key="uninstall",
                        label="Uninstall",
                        icon_name="cancel",
                        enabled=True,
                        selectable=True,
                        height_ratio=1.0,
                        layout="horizontal",
                        font_size=14,
                    )
                )
            else:
                entries.append(
                    IconMenuEntry(
                        key="installed_permanent",
                        label="Installed (required)",
                        icon_name="checkbox_checked",
                        enabled=True,
                        selectable=False,
                        height_ratio=1.0,
                        layout="horizontal",
                        font_size=14,
                    )
                )
        elif installing:
            # The install is running in the web process and BACK left it there.
            # Install is absent because a second one would be refused, and this
            # row is the only way back to the screen that can stop this one.
            entries.append(
                IconMenuEntry(
                    key="view",
                    label="View install progress",
                    icon_name="download",
                    enabled=True,
                    selectable=True,
                    height_ratio=1.0,
                    layout="horizontal",
                    font_size=14,
                )
            )
        elif resume_point is not None:
            # A paused install replaces Install rather than joining it. Starting
            # fresh would wipe the preserved tree, which is what Discard is for
            # saying out loud; offering both would make that destruction a side
            # effect of the innocuous-looking choice.
            entries.append(
                IconMenuEntry(
                    key="resume",
                    label=f"Resume install ({resume_point.percent}%)",
                    icon_name="download",
                    enabled=True,
                    selectable=True,
                    height_ratio=1.0,
                    layout="horizontal",
                    font_size=14,
                )
            )
            entries.append(
                IconMenuEntry(
                    key="discard",
                    label="Discard paused install",
                    icon_name="cancel",
                    enabled=True,
                    selectable=True,
                    height_ratio=1.0,
                    layout="horizontal",
                    font_size=14,
                )
            )
        else:
            install_label = f"Install (~{est_minutes} min)"
            entries.append(
                IconMenuEntry(
                    key="install",
                    label=install_label,
                    icon_name="download",
                    enabled=True,
                    selectable=True,
                    height_ratio=1.0,
                    layout="horizontal",
                    font_size=14,
                )
            )

        return entries

    def handle_selection(result: MenuSelection):
        est_minutes = engine_info.get("estimated_install_minutes", 5)

        if result.key == "install":
            show_install_progress(
                engine_name, display_name, est_minutes, board, log,
                **screen_arguments(),
            )
            return None

        if result.key == "view":
            watch_progress(
                engine_name, display_name, est_minutes, board, log,
                **screen_arguments(),
            )
            return None

        if result.key == "resume":
            point = resume_store.read(engine_name)
            if point is None:
                # Discarded from the web between the screen being drawn and the
                # press. Redraw rather than start a fresh build the user did not
                # ask for.
                return None
            show_install_progress(
                engine_name, display_name, est_minutes, board, log,
                resume_at_ref=point.ref, **screen_arguments(),
            )
            return None

        if result.key == "discard":
            if not confirm_discard_install(menu_manager, display_name):
                return None
            log.info(f"[EngineManager] Discarding the paused {engine_name} install")
            discarded = control.discard(engine_name)
            if discarded.accepted:
                board.beep(board.SOUND_GENERAL)
            else:
                # The owner refused -- most likely because that engine started
                # installing again from the web while this screen was open.
                log.warning(f"[EngineManager] Discard refused: {discarded.message}")
                board.beep(board.SOUND_GENERAL, event_type="error")
                _show_splash(board, log, f"Could not discard\n\n{discarded.message}",
                             f"{display_name} discard")
                time.sleep(_MESSAGE_DWELL_SECONDS)
            return None

        if result.key == "uninstall":
            log.info(f"[EngineManager] Uninstalling {engine_name}")
            board.display_manager.clear_widgets(addStatusBar=False)
            uninstall_splash = SplashScreen(
                board.display_manager.update,
                message=f"Uninstalling\n{display_name}...",
                leave_room_for_status_bar=False,
            )
            _wait_for_splash(
                board.display_manager.add_widget(uninstall_splash),
                log,
                f"{display_name} uninstall",
            )

            engine_manager.uninstall_engine(engine_name)
            uninstall_splash.set_message(f"{display_name}\nuninstalled")
            time.sleep(1)
            return MenuSelection("BACK", 0)

        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=2)


def handle_engine_manager_menu(
    menu_manager,
    board,
    log,
    handle_detail_menu: Callable[[dict], Optional[MenuSelection]],
) -> Optional[MenuSelection]:
    """Handle engine manager submenu.

    Shows list of available engines with installation status and summary.

    Args:
        menu_manager: Menu manager instance
        board: Board module
        log: Logger instance
        handle_detail_menu: Callback to handle engine detail menu

    Returns:
        MenuSelection if break, None otherwise
    """
    from universalchess.managers.engine_manager import get_engine_manager

    engine_manager = get_engine_manager()

    def build_entries():
        entries = []
        engines = engine_manager.get_engine_list()
        engines_sorted = sorted(engines, key=lambda e: (not e["installed"], e["display_name"]))

        for engine in engines_sorted:
            installed = engine["installed"]
            icon = "checkbox_checked" if installed else "checkbox_empty"
            est_minutes = engine.get("estimated_install_minutes", 5)
            summary = engine.get("summary", "")
            description = engine.get("description", "")
            
            # Create a short teaser from the full description (first ~60 chars)
            teaser = description[:60] + "..." if len(description) > 60 else description

            # Label: name (+ install time) on first line, summary on second line
            if installed:
                label = f"{engine['display_name']}\n{summary}"
            else:
                label = f"{engine['display_name']} (~{est_minutes}m)\n{summary}"

            entries.append(
                IconMenuEntry(
                    key=engine["name"],
                    label=label,
                    icon_name=icon,
                    enabled=True,
                    selectable=True,
                    height_ratio=2.0,
                    layout="horizontal",
                    font_size=11,
                    description=teaser,
                    description_font_size=10,
                )
            )

        return entries

    def handle_selection(result: MenuSelection):
        engine_name = result.key
        engines = engine_manager.get_engine_list()
        engine_info = next((e for e in engines if e["name"] == engine_name), None)
        if not engine_info:
            return None

        sub_result = handle_detail_menu(engine_info)
        if is_break_result(sub_result):
            return sub_result

        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=0)


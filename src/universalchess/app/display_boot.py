"""Bring the e-paper panel up and put a splash on it, before anything else.

Two controllers ship behind the same panel connector, and which one is present
can only be learned by probing. The probe is expensive on the wrong controller
(a 5.1 s BUSY timeout measured on a Pi Zero), so the outcome is written to disk
and used as the next boot's hint, while the fallback stays in both directions so
a panel swap self-corrects.

This runs before the application module is imported: the splash it puts on the
panel is what the user watches during the slow imports and the controller
handshake that follow.
"""

import os
from typing import Optional, Tuple

from universalchess.board.display_settings import read_flag, read_waveform_profile_key
from universalchess.board.logging import log
from universalchess.epaper import Manager, SplashScreen
from universalchess.i18n import t


def wait_for_display_promise(promise, operation_name: str, timeout: float = 10.0):
    """Wait for a display promise in the background and log any errors.

    This allows the main thread to continue while display operations complete.
    Errors are logged but don't block startup.

    Args:
        promise: The Future to wait on
        operation_name: Description of the operation for logging
        timeout: Maximum time to wait in seconds
    """
    import threading

    def _wait():
        try:
            if promise:
                result = promise.result(timeout=timeout)
                log.debug(f"[Display] {operation_name} completed: {result}")
        except Exception as e:
            log.warning(f"[Display] {operation_name} failed: {e}")

    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()


def on_display_refresh(image, red_image=None):
    """Callback for display refreshes - writes image to web static folder.

    Used by the web dashboard to mirror the e-paper display. In three-color mode
    the RED-plane snapshot is forwarded so the mirror is composed in RGB
    (white/black/red); ``red_image`` is None for mono/fast-B/W refreshes.

    After the snapshot is written an ``epaper_changed`` event is pushed to the
    web so the board-control page reloads ``/screen.jpg`` (a single JPEG) instead
    of streaming MJPEG, which iPad Safari will not render inside an ``<img>``.
    The file mtime is sent as the browser's cache-busting token.
    """
    try:
        from universalchess.services.chromecast import write_epaper_jpg
        from universalchess.services.game_broadcast import broadcast_epaper_changed
        path = write_epaper_jpg(image, red_image=red_image)
        broadcast_epaper_changed(os.stat(path).st_mtime)
    except Exception as e:
        log.debug(f"Failed to write epaper.jpg: {e}")


def attempt_display_init(epd, batch_updates: bool = True):
    """Build a Manager around ``epd`` and run initialize(); never raises.

    Returns ``(manager, DisplayAttempt)``. A failed init is reported as an
    attempt rather than an exception so the selector can decide whether to fall
    back. ``busy_timeout`` is read from the driver's flag so a BUSY-timeout
    failure (the inverted-polarity V1 signature) is distinguished from any other
    initialization error. ``batch_updates`` is injected into the Manager so the
    scheduler ships with the configured update-batching behavior.
    """
    from universalchess.board.display_selection import DisplayAttempt
    manager = Manager(on_refresh=on_display_refresh, epd=epd,
                      batch_updates=batch_updates)
    try:
        promise = manager.initialize()
        # Don't block - monitor in background thread
        wait_for_display_promise(promise, "initialize", timeout=10.0)
        return manager, DisplayAttempt(ok=True)
    except Exception as e:
        return manager, DisplayAttempt(
            ok=False,
            busy_timeout=getattr(epd, "busy_timeout_occurred", False),
            error=str(e),
        )


def build_epd(controller: str, key: str, three_color: bool):
    """Construct the driver for ``controller``, resolving its waveform profile.

    The driver module is imported here rather than at function entry so a board
    only pays the import cost of the controller it actually probes. The stored
    ``waveform_profile`` key is resolved against the controller's own profile
    family, so each driver gets a profile it understands (falling back to its
    verified table when the key belongs to the other controller or to a
    replacement panel). ``three_color`` is ignored: the Centaur glass has no
    red ink, and a stored switch may be left on from GDEY029Z95.
    """
    from universalchess.board import display_selection as ds
    from universalchess.epaper.framework.waveshare import panel_models
    from universalchess.epaper.framework.waveshare import waveform_profiles as wp

    automatic = panel_models.get_model(panel_models.PANEL_AUTO)
    three_color = three_color and automatic.three_color_supported
    if controller == ds.CONTROLLER_SSD1680:
        from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import EPD
        profile = panel_models.waveform_for(automatic, key, wp.CONTROLLER_SSD16XX)
    else:
        from universalchess.epaper.framework.waveshare.epd2in9d import EPD
        profile = panel_models.waveform_for(automatic, key, wp.CONTROLLER_UC8151D)
    return EPD(profile=profile, three_color=three_color), profile


def build_panel_epd(model, key: str, three_color: bool, partial_only: bool = False):
    """Construct the driver for an explicit replacement panel.

    ``model`` is a ``panel_models.PanelModel`` with ``explicit`` True. The
    waveform is ``panel_models.waveform_for``, the same one a live tuning
    change loads. ``three_color`` is ignored on a panel without red ink: on
    GDEW029I6FD 0x13 is the second gray plane. The driver is given the model,
    so it reports this glass and its capabilities.
    """
    from universalchess.epaper.framework.waveshare import panel_models

    three_color = three_color and model.three_color_supported
    profile = panel_models.waveform_for(model, key, None)
    if model.driver_module == "epd2in9d":
        from universalchess.epaper.framework.waveshare.epd2in9d import EPD
        return EPD(profile=profile, three_color=three_color,
                   panel=model, partial_only=partial_only)
    if model.driver_module == "epd2in9_ssd1680":
        from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import EPD
        return EPD(profile=profile, three_color=three_color, panel=model)
    if model.driver_module == "epd2in9_ssd1685":
        from universalchess.epaper.framework.waveshare.epd2in9_ssd1685 import EPD
        return EPD(panel=model)
    if model.driver_module == "epd2in9_jd7966x":
        from universalchess.epaper.framework.waveshare.epd2in9_jd7966x import EPD, spec_for
        return EPD(spec_for(model.key), three_color=three_color)
    raise ValueError(f"no driver for panel {model.key}")


def switch_to_panel(manager, model, key: str, three_color: bool, partial_only: bool) -> bool:
    """Drive an explicit replacement panel live, and report it to the web process.

    The Manager initializes the new driver and keeps the current one when
    that fails (the chosen glass may not be connected). Only a driver that
    initialized is written to the display-status file; the web System card
    and the waveform validation read the controller from there.
    """
    from universalchess.board import hardware_info

    epd = build_panel_epd(model, key, three_color, partial_only)
    if not manager.replace_epd(epd):
        log.warning(f"[Display] {model.key} did not initialize; keeping the current panel driver")
        return False
    hardware_info.write_display_status(
        initialized=True, busy_timeout=False, controller=model.controller, panel=model.key,
    )
    return True


def switch_to_automatic(manager, key: str, three_color: bool) -> bool:
    """Return to the Centaur V1/V2 probe live, and report the controller that answered.

    Tries the controllers in the boot probe order. The Manager keeps the
    current driver when a candidate does not initialize, so a failed probe
    leaves the panel as it was.
    """
    from universalchess.board import display_selection as ds
    from universalchess.board import hardware_info
    from universalchess.epaper.framework.waveshare import panel_models

    prior = hardware_info.read_display_status()
    for controller in ds.controller_order(ds.hint_from_status(prior)):
        epd, _profile = build_epd(controller, key, three_color)
        if manager.replace_epd(epd):
            hardware_info.write_display_status(
                initialized=True, busy_timeout=False, controller=controller,
                panel=panel_models.PANEL_AUTO,
            )
            return True
    log.warning("[Display] Automatic panel probe failed; keeping the current panel driver")
    return False


def init_display() -> Tuple[Manager, Optional[SplashScreen]]:
    """Initialize the display and show the splash, before board initialization.

    Probes the controller that drove the panel on the previous boot first, read
    back from the status file this function itself writes. A V1 panel can never
    satisfy the UC8151D probe, so without the hint every boot pays the full BUSY
    timeout (5.1 s measured on a Pi Zero) to re-derive a fact already on disk.
    A board with no usable history keeps the shipped UC8151D-first order.

    The fallback to the other controller is preserved in both directions and
    requires no opt-in, so a stale hint (panel swap, restored config) always
    self-corrects rather than leaving the panel blank. The [display]
    waveform_profile setting does not affect which controller is chosen, only
    how the chosen driver drives the panel. The resolved outcome
    (controller + busy_timeout) is published to the cross-process status file
    for the web System card, and is what seeds the next boot's hint.

    Display operations are queued and monitored in background threads, allowing
    the main thread to continue with other startup tasks while the e-paper
    display catches up (the initial Clear() takes ~3 seconds).

    Returns:
        ``(manager, splash)``. When no controller could drive the panel the
        splash is None and the manager is the one whose init failed
        (``manager.initialized`` is False): it drives no panel and sends each
        screen to the web mirror, and startup continues headless on it.
        Returning no manager sent the application into its own display retry,
        which raised and ended startup.

    The previous boot's mirror frame is discarded first, so the remote view
    shows no screen until this boot draws one, never an earlier one.
    """
    from universalchess.board import hardware_info
    from universalchess.board import display_selection as ds
    from universalchess.services.chromecast import discard_epaper_jpg

    discard_epaper_jpg()
    key = read_waveform_profile_key()
    three_color = read_flag('three_color')
    batch_updates = read_flag('batch_updates', default=True)

    from universalchess.board import display_settings
    from universalchess.epaper.framework.waveshare import panel_models
    model = panel_models.get_model(display_settings.read_panel_key())
    if model.explicit:
        # An explicit Good Display module does not fall through to the other
        # Centaur controller. The two command sets are not interchangeable.
        epd = build_panel_epd(model, key, three_color, read_flag('partial_only'))
        manager, primary = attempt_display_init(epd, batch_updates=batch_updates)
        if primary.ok:
            outcome = ds.DisplayOutcome(
                initialized=True,
                busy_timeout=False,
                active_controller=model.controller,
                error=None,
            )
        else:
            outcome = ds.DisplayOutcome(
                initialized=False,
                busy_timeout=primary.busy_timeout,
                active_controller=None,
                error=primary.error,
            )
        _log_display_event(ds.explicit_panel_log_entry(outcome, primary, model.event_label))
        return _finish_display_init(manager, outcome, model.key)

    prior = hardware_info.read_display_status()
    hint = ds.hint_from_status(prior)
    order = ds.controller_order(hint)
    # Carried forward only when the hint skips the UC8151D probe: that driver is
    # the only one that can observe the V1 BUSY-timeout signature.
    prior_busy_timeout = bool(prior.get('busy_timeout')) if prior else False
    first, second = order
    hinted = first != ds.CONTROLLER_UC8151D
    if hint:
        log.info(f"Display: probing {hint} first (drove the panel last boot)")

    epd, _ = build_epd(first, key, three_color)
    manager, primary = attempt_display_init(epd, batch_updates=batch_updates)
    alt = None
    if ds.should_attempt_alt(primary, hinted=hinted):
        alt_epd, alt_profile = build_epd(second, key, three_color)
        log.warning(
            f"{first} init failed at startup; trying {second} fallback "
            f"(profile={alt_profile.key})"
        )
        alt_manager, alt = attempt_display_init(
            alt_epd, batch_updates=batch_updates
        )
        if alt.ok:
            manager = alt_manager

    outcome = ds.resolve_outcome(
        primary, alt, order=order, prior_busy_timeout=prior_busy_timeout
    )
    _log_display_event(ds.event_log_entry(outcome, primary, alt, order))
    return _finish_display_init(manager, outcome, panel_models.PANEL_AUTO)


def _finish_display_init(manager, outcome, panel: str):
    """Publish the probe result and, on success, put the splash on the panel.

    Shared by the automatic V1/V2 probe and an explicit replacement panel.
    ``panel`` is the panel key recorded beside the controller, so the next
    boot's hint and the System card can tell Centaur glass from a
    replacement on the same chip. A failed init leaves the board headless
    and records that for the System card; Display tuning stays available
    either way.
    """
    from universalchess.board import hardware_info

    if not outcome.initialized:
        log.warning(f"Early display initialization failed: {outcome.error}")
        # Latch the failure for the (separate) web process: the panel never
        # initialized (e.g. a V1 panel that trips the BUSY timeout), so the
        # System card must show "Not responding" rather than the configured V2.
        # busy_timeout still propagates so the next boot can skip the UC8151D
        # probe; Display tuning stays offered regardless.
        hardware_info.write_display_status(
            initialized=False,
            error=outcome.error,
            busy_timeout=outcome.busy_timeout,
            controller=None,
            panel=panel,
        )
        return manager, None

    # Show splash screen immediately (full screen, no status bar)
    manager.clear_widgets(addStatusBar=False)
    splash = SplashScreen(manager.update, message=t("splash.starting"),
                          leave_room_for_status_bar=False, tagline=t("splash.tagline"))
    promise = manager.add_widget(splash)
    # Don't block - monitor in background thread
    wait_for_display_promise(promise, "add_splash", timeout=10.0)
    # Publish success so the web System card reflects the live panel state.
    hardware_info.write_display_status(
        initialized=True,
        busy_timeout=outcome.busy_timeout,
        controller=outcome.active_controller,
        panel=panel,
    )
    return manager, splash


def _log_display_event(entry) -> None:
    """Append this boot's ``(level, message)`` display line to the Settings diagnostics.

    Best-effort: :func:`log_event` never raises. Lazy-imports the event log so
    a missing logger cannot block the splash.
    """
    from universalchess.board import display_selection as ds
    from universalchess.services.event_log import log_event

    level, message = entry
    log_event(ds.EVENT_CATEGORY, message, level=level)

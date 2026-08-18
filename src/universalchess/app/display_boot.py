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


def read_display_flag(name: str, default: bool = False) -> bool:
    """Return whether a [display] boolean opt-in is set.

    Used for the experimental high_contrast drive-voltage override (default off)
    and the navigation-batching option (default on). high_contrast does not gate
    any driver selection -- it only adjusts how the active driver drives the
    panel (SSD1680 source/VCOM push, or UC8151D VCOM_DC bump). ``default`` is the
    value when the key is absent, so a never-configured board gets the intended
    shipped behavior (e.g. batching on).
    """
    from universalchess.board.settings import Settings
    value = Settings.read('display', name, 'True' if default else 'False')
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


def read_display_selection() -> Tuple[str, bool]:
    """Return ``(waveform_profile_key, high_contrast)`` from the [display] settings.

    Returns the raw stored key (resolved to a concrete profile only later, per
    the active controller). One key is shared across both controllers; each
    driver resolves it against its own family via
    ``waveform_profiles.get_profile(key, controller)``, falling back to that
    controller's verified default when the stored key belongs to the other
    controller (e.g. after a panel swap) -- so a working panel is never left
    without a waveform.
    """
    from universalchess.board.settings import Settings
    key = str(Settings.read('display', 'waveform_profile', '')).strip()
    return key, read_display_flag('high_contrast')


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


def build_epd(controller: str, key: str, high_contrast: bool, three_color: bool):
    """Construct the driver for ``controller``, resolving its waveform profile.

    The driver module is imported here rather than at function entry so a board
    only pays the import cost of the controller it actually probes. The stored
    ``waveform_profile`` key is resolved against the controller's own profile
    family, so each driver gets a profile it understands (falling back to its
    verified table when the key belongs to the other controller).
    """
    from universalchess.board import display_selection as ds
    from universalchess.epaper.framework.waveshare import waveform_profiles as wp

    if controller == ds.CONTROLLER_SSD1680:
        from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import EPD
        profile = wp.get_profile(key, wp.CONTROLLER_SSD16XX)
    else:
        from universalchess.epaper.framework.waveshare.epd2in9d import EPD
        profile = wp.get_profile(key, wp.CONTROLLER_UC8151D)
    return EPD(
        profile=profile,
        high_contrast=high_contrast,
        three_color=three_color,
    ), profile


def init_display() -> Tuple[Optional[Manager], Optional[SplashScreen]]:
    """Initialize the display and show the splash, before board initialization.

    Probes the controller that drove the panel on the previous boot first, read
    back from the status file this function itself writes. A V1 panel can never
    satisfy the UC8151D probe, so without the hint every boot pays the full BUSY
    timeout (5.1 s measured on a Pi Zero) to re-derive a fact already on disk.
    A board with no usable history keeps the shipped UC8151D-first order.

    The fallback to the other controller is preserved in both directions and
    requires no opt-in, so a stale hint (panel swap, restored config) always
    self-corrects rather than leaving the panel blank. The [display]
    waveform_profile / high_contrast settings do not affect which controller is
    chosen, only how the chosen driver drives the panel. The resolved outcome
    (controller + busy_timeout) is published to the cross-process status file
    for the web System card, and is what seeds the next boot's hint.

    Display operations are queued and monitored in background threads, allowing
    the main thread to continue with other startup tasks while the e-paper
    display catches up (the initial Clear() takes ~3 seconds).

    Returns:
        ``(manager, splash)``, both None when no controller could drive the
        panel -- startup continues headless rather than failing.
    """
    from universalchess.board import hardware_info
    from universalchess.board import display_selection as ds

    key, high_contrast = read_display_selection()
    three_color = read_display_flag('three_color')
    batch_updates = read_display_flag('batch_updates', default=True)

    prior = hardware_info.read_display_status()
    hint = ds.hint_from_status(prior)
    order = ds.controller_order(hint)
    # Carried forward only when the hint skips the UC8151D probe: that driver is
    # the only one that can observe the V1 BUSY-timeout signature, and the flag
    # gates the web UI's display-tuning card.
    prior_busy_timeout = bool(prior.get('busy_timeout')) if prior else False
    first, second = order
    hinted = first != ds.CONTROLLER_UC8151D
    if hint:
        log.info(f"Display: probing {hint} first (drove the panel last boot)")

    epd, _ = build_epd(first, key, high_contrast, three_color)
    manager, primary = attempt_display_init(epd, batch_updates=batch_updates)
    alt = None
    if ds.should_attempt_alt(primary, hinted=hinted):
        alt_epd, alt_profile = build_epd(second, key, high_contrast, three_color)
        log.warning(
            f"{first} init failed at startup; trying {second} fallback "
            f"(profile={alt_profile.key}, high_contrast={high_contrast})"
        )
        alt_manager, alt = attempt_display_init(
            alt_epd, batch_updates=batch_updates
        )
        if alt.ok:
            manager = alt_manager

    outcome = ds.resolve_outcome(
        primary, alt, order=order, prior_busy_timeout=prior_busy_timeout
    )

    if not outcome.initialized:
        log.warning(f"Early display initialization failed: {outcome.error}")
        # Latch the failure for the (separate) web process: the panel never
        # initialized (e.g. a V1 panel that trips the BUSY timeout), so the
        # System card must show "Not responding" rather than the configured V2.
        # busy_timeout still propagates so the UI reveals the display-tuning card.
        hardware_info.write_display_status(
            initialized=False,
            error=outcome.error,
            busy_timeout=outcome.busy_timeout,
            controller=None,
        )
        return None, None

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
    )
    return manager, splash

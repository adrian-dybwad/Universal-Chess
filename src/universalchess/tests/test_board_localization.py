"""The board's remaining screens are drawn in the device's language.

Why these tests exist
---------------------
The Lichess screens were localized first, which left the rest of the board still
building its text from English literals: the shutdown countdown a long PLAY hold
draws, the inactivity countdown, the startup and power splashes, the engine
manager, the Bluetooth and About lists, the WiFi panel, and the paged help
footer. On a Spanish board every one of those appeared in English, in the middle
of an otherwise translated menu, with nothing raised or logged.

:mod:`test_board_strings_are_localized` proves no literal is handed to a screen.
These prove the other half, which a scan cannot see: that the string a screen
asks for is the right one, with its numbers substituted in. They read Spanish
because it differs from English everywhere under test, so a fallback to English
cannot pass.
"""

from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from universalchess.board import board
from universalchess.board.system_info import DiskSnapshot, MemorySnapshot, SystemInfo
from universalchess.epaper.paged_text import PagedTextWidget
from universalchess.epaper.wifi_info import format_status_label
from universalchess.menus.about_menu import build_system_info_entries
from universalchess.menus.bluetooth_menu import keyboard_rows
from universalchess.menus.catalog.loader import get_localized_catalog
from universalchess.menus.engine_manager_menu import confirm_discard_install

COUNTDOWN_SECONDS = 3
SHIPPED_LOCALES = ("en", "es", "fr", "de", "nl", "pl", "it")


def _with_locale(monkeypatch, locale, read):
    """Read a bundle string in ``locale``, leaving no cached locale behind.

    The bundle memoises the device language on first use, so a test that
    switched it without resetting would decide the language of every test that
    ran afterwards.
    """
    from universalchess import i18n

    monkeypatch.setattr(
        "universalchess.services.language_service.get_language", lambda: locale
    )
    i18n._active_locale = None
    i18n._bundles.clear()
    i18n.refresh_active_language()
    try:
        return read(i18n)
    finally:
        i18n._active_locale = None
        i18n._bundles.clear()


class _FakeMenuManager:
    """Records the entries it is shown and refuses the destructive row."""

    def __init__(self, choice: str = "Cancel"):
        self.entries = []
        self._choice = choice

    def show_menu(self, entries, initial_index=0):
        self.entries = entries
        return self._choice


@pytest.fixture
def countdown_board(monkeypatch):
    """``board.shutdown_countdown`` with no hardware, no sleeps and a fake splash.

    The splash is a MagicMock so the message it is built with, and every message
    set on it afterwards, can be read back. PLAY is never queued, so the
    countdown runs to the end and draws every second.
    """
    splash = MagicMock()
    splash_class = MagicMock(return_value=splash)
    display_manager = MagicMock()
    display_manager.add_widget.return_value = Future()
    display_manager.add_widget.return_value.set_result("ok")

    monkeypatch.setattr(board, "controller", MagicMock(get_next_key=lambda timeout=0.0: None))
    monkeypatch.setattr(board, "display_manager", display_manager)
    monkeypatch.setattr(board, "beep", lambda *a, **k: None)
    monkeypatch.setattr(board.time, "sleep", lambda s: None)
    monkeypatch.setattr("universalchess.epaper.SplashScreen", splash_class)

    return {"splash": splash, "splash_class": splash_class}


def test_the_shutdown_countdown_counts_down_in_the_device_language(
    spanish_board, countdown_board
):
    """The hold-PLAY countdown draws Spanish text and the seconds remaining.

    Why: this is the screen the report named. It was an f-string, so a Spanish
    board counted down in English. How a regression manifests: the first frame
    reads "Shutdown in", or the seconds stop being substituted and the row shows
    a bare ``{seconds}``.
    """
    assert board.shutdown_countdown(countdown_seconds=COUNTDOWN_SECONDS) is True

    opening = countdown_board["splash_class"].call_args.kwargs["message"]
    assert opening == spanish_board.t("power.shutdown_in", seconds=COUNTDOWN_SECONDS)
    assert "Apagado" in opening and str(COUNTDOWN_SECONDS) in opening

    ticks = [call.args[0] for call in countdown_board["splash"].set_message.call_args_list]
    assert ticks == [
        spanish_board.t("power.shutdown_in", seconds=second)
        for second in range(COUNTDOWN_SECONDS, 0, -1)
    ]


def test_the_bluetooth_list_says_scanning_and_empty_in_the_device_language(spanish_board):
    """The keyboard scan's two placeholder rows are translated.

    Why: both were literals, so a Spanish board scanning for a keyboard showed
    an English "Scanning..." that then became an English "No devices". How a
    regression manifests: either row reverts to its English wording, or the two
    stop differing and the user cannot tell a finished scan from a running one.
    """
    scanning = keyboard_rows([], scanning=True)
    finished = keyboard_rows([], scanning=False)

    assert [row.label for row in scanning] == [spanish_board.t("common.scanning")]
    assert [row.label for row in finished] == [spanish_board.t("common.no_devices")]
    assert scanning[0].label != finished[0].label
    assert scanning[0].key == "__scanning__" and finished[0].key == "__none__"


def test_the_about_rows_label_their_readings_in_the_device_language(spanish_board):
    """About's four telemetry rows translate the label and keep the reading.

    Why: each row was an f-string pairing an English word with a number, so the
    numbers were right and the words were English. How a regression manifests:
    a row reads "Memory" on a Spanish board, or the translation swallows the
    reading and the row shows a label with no value under it.
    """
    info = SystemInfo(
        hostname="dgt-64",
        cpu_percent=12.5,
        cpu_temperature_celsius=44.0,
        memory=MemorySnapshot(used_bytes=512 * 1024 * 1024, total_bytes=1024 * 1024 * 1024, percent=50.0),
        disk=DiskSnapshot(used_bytes=3 * 1024**3, total_bytes=8 * 1024**3, percent=37.5),
        uptime_seconds=3600.0,
        load_average_1m=0.5,
    )

    labels = [row.label for row in build_system_info_entries(info)]

    assert len(labels) == 4
    assert labels[1].startswith("Memoria\n") and labels[2].startswith("Almacenamiento\n")
    assert labels[3].startswith("Tiempo activo\n")
    # Every row still carries its reading on the second line.
    assert all(len(label.split("\n", 1)) == 2 and label.split("\n", 1)[1] for label in labels)


def test_the_wifi_panel_reports_its_state_in_the_device_language(spanish_board):
    """The WiFi panel's three status lines are translated, signal included.

    Why: "Not connected", "WiFi enabled/disabled" and the signal line were
    literals in a panel whose surrounding menu was translated. How a regression
    manifests: a Spanish board shows English status under a Spanish heading, or
    the signal percentage stops being substituted.
    """
    off = format_status_label({"connected": False, "enabled": False})
    idle = format_status_label({"connected": False, "enabled": True})
    connected = format_status_label(
        {"connected": True, "ssid": "casa", "ip_address": "10.0.0.2", "signal": 72, "frequency": "5 GHz"}
    )

    assert off == spanish_board.t("wifi.disabled")
    assert idle.split("\n") == [
        spanish_board.t("common.not_connected"),
        spanish_board.t("wifi.enabled"),
    ]
    assert connected.split("\n") == ["casa", "10.0.0.2", spanish_board.t("wifi.signal", percent=72), "5 GHz"]


def test_the_paged_help_footer_numbers_its_pages_in_the_device_language(spanish_board):
    """The paged-text footer translates "Page x of y" and keeps both numbers.

    Why: help and coach text page through this footer, which was an f-string, so
    the only English left on an otherwise Spanish help screen was its footer.
    How a regression manifests: the footer reads "Page 1 of 3", or a number goes
    missing and the user cannot tell how much text is left.
    """
    widget = PagedTextWidget(0, 0, 128, 120, lambda *args, **kwargs: None)
    widget.set_text("hola " * 400)

    assert widget.page_count > 1, "the sample text must span more than one page"
    assert widget.footer_label == spanish_board.t(
        "common.page_of", current=widget.current_page, total=widget.page_count
    )
    assert widget.footer_label.startswith("Página 1 de ")


def test_the_discard_prompt_and_its_rows_are_in_the_device_language(spanish_board):
    """The engine-manager discard confirmation is translated, engine name kept.

    Why: discarding a paused build cannot be undone, and every row of the
    confirmation -- the question, Discard and Cancel -- was an English literal.
    A user who cannot read the prompt is being asked to confirm a deletion they
    have not understood. How a regression manifests: any row reverts to English,
    or the engine's name drops out of the question and the prompt no longer says
    what is being discarded.
    """
    menu = _FakeMenuManager(choice="Cancel")

    assert confirm_discard_install(menu, "Stockfish") is False

    labels = [entry.label for entry in menu.entries]
    assert labels == [
        spanish_board.t("engine.discard_paused_named", engine="Stockfish"),
        spanish_board.t("engine.discard"),
        spanish_board.t("common.cancel"),
    ]
    assert "Stockfish" in labels[0]
    assert labels[1] == "Descartar" and labels[2] == "Cancelar"


@pytest.mark.parametrize("locale", SHIPPED_LOCALES)
def test_the_queen_warning_help_quotes_the_words_the_panel_draws(locale, monkeypatch):
    """The queen-threat help quotes the alert as the alert panel draws it.

    Why: the help explains the warning by quoting it, and the panel drew English
    when the overlays were written. Localizing the panel left all three overlays
    quoting YOUR QUEEN while the board drew IHRE DAME, TU DAMA or VOTRE DAME, so
    the help named words that never appear on screen -- a drift a coverage audit
    cannot see, because both strings are translated, just not to each other. How
    a regression manifests: the drawn wording is missing from that locale's help.
    """
    drawn = _with_locale(monkeypatch, locale, lambda i18n: i18n.t("alert.your_queen"))
    help_text = get_localized_catalog(locale).get_node("alerts.queen_threat")["help"]

    assert drawn.replace("\n", " ") in help_text


@pytest.mark.parametrize("locale", SHIPPED_LOCALES)
def test_the_play_help_quotes_the_word_the_tile_shows_mid_game(locale):
    """Play's help quotes the label the tile itself carries once a game is on.

    Why: the help says which word replaces PLAY while a game is in progress, so
    it quotes a label translated a few lines above it in the same overlay. The
    German pair was the live risk: the tile reads WEITER because FORTSETZEN does
    not fit at 32 pt, and a help text translated independently would have said
    FORTSETZEN. How a regression manifests: the two disagree and the help points
    at a word the tile never shows.
    """
    node = get_localized_catalog(locale).get_node("main.play")

    assert node["label_in_progress"] in node["help"]

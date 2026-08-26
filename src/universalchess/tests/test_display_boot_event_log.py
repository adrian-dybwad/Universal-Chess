"""Display probe results are written to the Settings diagnostics event log.

Why these tests exist:
    Overlay-missing, gpiochip/spidev permission, and other non-timeout init
    failures used to live only in /var/log/modmenuoutput.log. The Event Log is
    the operator-facing trail. These tests pin that init_display emits one
    ``display`` event per boot for a hardware failure and for a detected panel.

How a regression manifests:
    - SPI/GPIO errors never appear in Settings -> Diagnostics.
    - A successful probe no longer records which controller initialized.
"""

from unittest.mock import MagicMock

from universalchess.app import display_boot
from universalchess.board.display_selection import DisplayAttempt
from universalchess.services import event_log


def _stub_probe(monkeypatch, attempt, *, splash=False):
    monkeypatch.setattr(display_boot, "read_selection", lambda: ("", False))
    monkeypatch.setattr(display_boot, "read_flag", lambda *a, **k: False)
    monkeypatch.setattr(
        display_boot, "build_epd", lambda *a, **k: (MagicMock(), MagicMock(key="x"))
    )
    monkeypatch.setattr(
        display_boot, "attempt_display_init", lambda *a, **k: (MagicMock(), attempt)
    )
    hi = MagicMock()
    hi.read_display_status.return_value = None
    monkeypatch.setattr(
        "universalchess.board.hardware_info.read_display_status",
        hi.read_display_status,
    )
    monkeypatch.setattr(
        "universalchess.board.hardware_info.write_display_status",
        hi.write_display_status,
    )
    if splash:
        monkeypatch.setattr(display_boot, "wait_for_display_promise", lambda *a, **k: None)
        monkeypatch.setattr(display_boot, "SplashScreen", MagicMock())
        monkeypatch.setattr(display_boot, "t", lambda key: key)
    return hi


def test_init_display_logs_overlay_failure_as_error(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(log_path))
    monkeypatch.setattr(event_log, "_loggers", {})
    err = (
        "Failed to initialize display: e-paper SPI is spi-gpio; "
        "overlay not loaded (no spi-gpio SPI master)"
    )
    _stub_probe(
        monkeypatch,
        DisplayAttempt(ok=False, busy_timeout=False, error=err),
    )

    manager, splash = display_boot.init_display()

    assert manager is None
    assert splash is None
    events = event_log.read_events(path=log_path)
    assert len(events) == 1
    assert events[0]["category"] == "display"
    assert events[0]["level"] == "error"
    assert "overlay not loaded" in events[0]["message"]
    assert "UC8151D" in events[0]["message"]


def test_init_display_logs_detected_v2_panel(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(log_path))
    monkeypatch.setattr(event_log, "_loggers", {})
    _stub_probe(monkeypatch, DisplayAttempt(ok=True), splash=True)

    display_boot.init_display()

    events = event_log.read_events(path=log_path)
    assert len(events) == 1
    assert events[0]["category"] == "display"
    assert events[0]["level"] == "info"
    assert events[0]["message"] == "E-paper panel detected: UC8151D (V2)"


def test_init_display_logs_detected_v1_after_busy_timeout(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(log_path))
    monkeypatch.setattr(event_log, "_loggers", {})
    results = [
        (MagicMock(), DisplayAttempt(ok=False, busy_timeout=True, error="busy timeout")),
        (MagicMock(), DisplayAttempt(ok=True)),
    ]

    def fake_attempt(*_a, **_k):
        return results.pop(0)

    monkeypatch.setattr(display_boot, "read_selection", lambda: ("", False))
    monkeypatch.setattr(display_boot, "read_flag", lambda *a, **k: False)
    monkeypatch.setattr(
        display_boot, "build_epd", lambda *a, **k: (MagicMock(), MagicMock(key="x"))
    )
    monkeypatch.setattr(display_boot, "attempt_display_init", fake_attempt)
    monkeypatch.setattr(
        "universalchess.board.hardware_info.read_display_status", lambda: None
    )
    monkeypatch.setattr(
        "universalchess.board.hardware_info.write_display_status", lambda **k: None
    )
    monkeypatch.setattr(display_boot, "wait_for_display_promise", lambda *a, **k: None)
    monkeypatch.setattr(display_boot, "SplashScreen", MagicMock())
    monkeypatch.setattr(display_boot, "t", lambda key: key)

    display_boot.init_display()

    events = event_log.read_events(path=log_path)
    assert len(events) == 1
    assert events[0]["message"] == "E-paper panel detected: SSD1680 (V1)"
    assert events[0]["level"] == "info"


def test_init_display_logs_no_panel_when_both_controllers_time_out(monkeypatch, tmp_path):
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(log_path))
    monkeypatch.setattr(event_log, "_loggers", {})
    results = [
        (MagicMock(), DisplayAttempt(ok=False, busy_timeout=True, error="busy timeout")),
        (MagicMock(), DisplayAttempt(ok=False, busy_timeout=True, error="ssd1680 busy")),
    ]

    def fake_attempt(*_a, **_k):
        return results.pop(0)

    monkeypatch.setattr(display_boot, "read_selection", lambda: ("", False))
    monkeypatch.setattr(display_boot, "read_flag", lambda *a, **k: False)
    monkeypatch.setattr(
        display_boot, "build_epd", lambda *a, **k: (MagicMock(), MagicMock(key="x"))
    )
    monkeypatch.setattr(display_boot, "attempt_display_init", fake_attempt)
    monkeypatch.setattr(
        "universalchess.board.hardware_info.read_display_status", lambda: None
    )
    monkeypatch.setattr(
        "universalchess.board.hardware_info.write_display_status", lambda **k: None
    )

    manager, splash = display_boot.init_display()

    assert manager is None
    assert splash is None
    events = event_log.read_events(path=log_path)
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert events[0]["message"].startswith("No e-paper panel detected")
    assert "ssd1680 busy" in events[0]["message"]

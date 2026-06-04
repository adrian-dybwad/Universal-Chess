#!/usr/bin/env python3
"""Tests for BleManager.start_async running BLE bring-up off the startup path.

Why this exists:
- BleManager.start() spends ~15s in configure_adapter_security() (three
  `sudo btmgmt` calls that block while bluetoothd owns the management socket),
  followed by D-Bus/agent/GATT/advertisement registration. main() used to call
  start() inline before launching the GLib mainloop thread, which froze the
  startup splash on the "BLE..." stage for that whole period.
- start_async() must run the IDENTICAL setup (same start() call, same mainloop)
  on a background daemon thread and return immediately, so the menu appears
  while BLE finishes initializing. It must also run the GLib mainloop only after
  a successful start(), and must NOT run it if start() reports failure.

How a regression manifests:
- If start_async() were to block (e.g. reverted to calling start() inline), the
  "non-blocking" assertions below fail because the caller cannot proceed until
  the (blocked) start() completes.
- If the mainloop were run regardless of start()'s result, the failure test
  would see run() called after start() returned False.
"""

import sys
import threading
from unittest.mock import MagicMock

# Stub the D-Bus and GObject-introspection stacks so the real BleManager module
# imports on non-hardware machines. These are only used for live BlueZ access,
# which start_async() is monkeypatched away from in these tests.
for _mod in ("dbus", "dbus.service", "dbus.mainloop", "dbus.mainloop.glib",
             "gi", "gi.repository"):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.managers.ble import BleManager


def _make_manager() -> BleManager:
    """Construct a BleManager. __init__ only sets attributes (no D-Bus access)."""
    return BleManager(device_name="TEST BOARD")


def test_start_async_returns_before_start_completes_then_runs_mainloop():
    """start_async() must not block the caller on the slow start(), and must run
    the mainloop once start() succeeds.

    Regression: if start() is run inline (the old behavior), the caller blocks
    for the full adapter-security duration. Here a fake start() blocks on an
    event; the test asserts the caller regained control (and that the mainloop
    had not yet run) while start() was still blocked, proving it is async.
    """
    manager = _make_manager()
    fake_loop = MagicMock()

    start_entered = threading.Event()
    release_start = threading.Event()

    def fake_start(mainloop):
        # Mirror the real start(): record the loop the mainloop thread will run.
        manager._mainloop = mainloop
        start_entered.set()
        # Block to emulate the ~15s adapter-security stall.
        assert release_start.wait(timeout=5.0), "test deadlock waiting to release start()"
        return True

    manager.start = fake_start

    thread = manager.start_async(fake_loop)

    # Caller regained control: start() must already be executing on the thread
    # while we are here, and the mainloop must not have run yet (start blocked).
    assert start_entered.wait(timeout=2.0), "start() was never invoked on the thread"
    assert fake_loop.run.call_count == 0, "mainloop ran before start() completed"
    assert thread.daemon is True, "BLE thread must be a daemon so it never blocks shutdown"

    # Let start() finish; the mainloop must then run exactly once, on this thread.
    release_start.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "BLE thread did not finish after start() returned"
    assert fake_loop.run.call_count == 1, "mainloop must run exactly once after start() succeeds"


def test_start_async_does_not_run_mainloop_when_start_fails():
    """If start() reports failure, the mainloop must not run.

    Regression: running the mainloop after a failed setup would spin a useless
    loop and mask the failure. fake start() returns False; run() must stay
    uncalled.
    """
    manager = _make_manager()
    fake_loop = MagicMock()

    def fake_start(mainloop):
        manager._mainloop = mainloop
        return False

    manager.start = fake_start

    thread = manager.start_async(fake_loop)
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "BLE thread did not finish"
    assert fake_loop.run.call_count == 0, "mainloop must not run when start() fails"


def test_start_async_survives_start_exception():
    """An exception in start() must not escape the thread (it would otherwise be
    lost and could leave the caller unaware), and the mainloop must not run.

    Regression: an unhandled exception on the BLE thread would terminate only
    that thread silently; this guards that start_async swallows-and-logs rather
    than running the mainloop on a half-initialized stack.
    """
    manager = _make_manager()
    fake_loop = MagicMock()

    def fake_start(mainloop):
        raise RuntimeError("simulated BlueZ failure")

    manager.start = fake_start

    thread = manager.start_async(fake_loop)
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "BLE thread did not finish after start() raised"
    assert fake_loop.run.call_count == 0, "mainloop must not run when start() raises"

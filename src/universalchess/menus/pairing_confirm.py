"""On-board confirmation for incoming Bluetooth pairings.

When a phone or chess app pairs to the board, BlueZ's pairing agent must decide
whether to proceed. Two things are required:

  1. Show the 6-digit numeric-comparison code on the board so the user can verify
     it matches the code shown on the other device.
  2. Require the user to explicitly press "Pair" on the board; otherwise anyone
     in range could silently pair.

This module holds the transport- and UI-agnostic decision logic so it can be
unit-tested without a live D-Bus connection or the e-paper stack. The D-Bus
agent (``managers.ble``) and the display modal (``managers.display``) are thin
adapters over these helpers.
"""

from typing import Callable, List, Optional

try:
    from universalchess.board.logging import log as _default_log
except ImportError:  # pragma: no cover - logging shim for isolated test envs
    import logging
    _default_log = logging.getLogger(__name__)

# Menu entry keys. INFO_KEY marks the non-selectable row that displays the code;
# PAIR_KEY / REJECT_KEY are the two actions the user can choose.
INFO_KEY = "pairing_info"
PAIR_KEY = "PAIR"
REJECT_KEY = "REJECT"


def is_pairing_accepted(menu_result: Optional[str]) -> bool:
    """Return True only when the user explicitly selected Pair.

    Every other outcome -- the Reject entry, a BACK press, the 30s ``TIMEOUT``,
    an externally injected ``CANCELLED``, or ``None`` -- denies the pairing. This
    is deliberately a strict allow-list so inactivity or a stray event can never
    authorize an unknown device.
    """
    return menu_result == PAIR_KEY


def build_pairing_confirm_entries(
    passkey: Optional[str],
    make_entry: Callable[[str, str, str, bool], object],
) -> List[object]:
    """Build the [info, Pair, Reject] rows for the confirmation screen.

    Args:
        passkey: Formatted numeric-comparison code to display, or ``None`` for a
            just-works pairing that carries no code to compare.
        make_entry: Factory ``(key, label, icon_name, selectable) -> entry``,
            injected so this stays free of the e-paper/resource imports and is
            unit-testable.

    The info row is non-selectable: it conveys the code/prompt only and must not
    be a focusable target, so the default highlight can rest on Reject.
    """
    if passkey:
        prompt = f"Pair device?\n{passkey}"
    else:
        prompt = "Pair this\ndevice?"
    return [
        make_entry(INFO_KEY, prompt, "bluetooth", False),
        make_entry(PAIR_KEY, "Pair", "play", True),
        make_entry(REJECT_KEY, "Reject", "cancel", True),
    ]


def run_pairing_confirmation(
    on_confirm: Optional[Callable[[Optional[str]], bool]],
    passkey: Optional[str],
    accept: Callable[[], None],
    reject: Callable[[], None],
    log=_default_log,
) -> None:
    """Resolve a pairing request into a single accept() or reject() action.

    Args:
        on_confirm: Shows the on-board prompt and returns True to pair, False to
            decline. ``None`` means there is no UI to ask (e.g. display not yet
            up), which is treated as a refusal.
        passkey: Numeric-comparison code forwarded to ``on_confirm`` for display,
            or ``None`` for just-works pairing.
        accept: Side-effecting callback invoked exactly once to authorize the
            pairing (the D-Bus agent's empty method reply).
        reject: Side-effecting callback invoked exactly once to refuse it (the
            D-Bus agent's ``org.bluez.Error.Rejected`` reply).

    A missing callback or any exception from ``on_confirm`` results in
    ``reject()`` so a UI failure can never silently authorize a pairing.
    """
    if on_confirm is None:
        log.warning("[Pairing] No confirmation UI available; rejecting pairing")
        reject()
        return
    try:
        accepted = bool(on_confirm(passkey))
    except Exception as exc:  # noqa: BLE001 - any failure must deny, not crash
        log.error(f"[Pairing] Confirmation prompt failed; rejecting: {exc}")
        reject()
        return
    if accepted:
        accept()
    else:
        reject()

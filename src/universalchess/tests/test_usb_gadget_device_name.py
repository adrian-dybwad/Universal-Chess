"""Tests for the name the USB gadget presents to the host computer.

The gadget's USB product string is the only name a user ever sees for this
connection: macOS lists the hardware port under it in Network settings and in
the Internet Sharing list, and Windows names the adapter from it. The stock Pi
kernel compiles in ``Raspberry Pi USB Gadget``, which says nothing about which
device on the desk it is -- and Shared mode's instructions ask the user to find
this device in that list and switch it off.

``g_ether`` takes the string as a module parameter, so a ``modprobe.d`` drop-in
sets it: the module is loaded from userspace (``modules-load=`` on the kernel
cmdline, or ``/etc/modules-load.d``), and both paths go through modprobe, which
reads ``/etc/modprobe.d``. Verified on a Zero 2 W that kmod keeps a quoted value
with spaces as one option:

    insmod .../g_serial.ko.xz iProduct="Universal Chess USB Gadget"

The name applies at the next boot. Nothing reloads ``g_ether`` to apply it
sooner: unloading it while a host is attached leaves the dwc2 core wedged in
``txfifo_flush``, and this is a cosmetic string.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from universalchess.menus.catalog.loader import get_localized_catalog, load_catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEB_ROOT = _REPO_ROOT / "packaging" / "deb-root"
_DROP_IN = _DEB_ROOT / "etc" / "modprobe.d" / "90-uc-usb-gadget-name.conf"

DEVICE_NAME = "Universal Chess USB Gadget"
GADGET_MODULE = "g_ether"


def _module_options(text: str) -> dict[str, dict[str, str]]:
    """Return ``{module: {option: value}}`` for the ``options`` lines of a file.

    Parsed with shlex so the assertions see what kmod sees: a quoted value with
    spaces is one option, an unquoted one is several.
    """
    parsed: dict[str, dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.startswith("options "):
            continue
        _, module, *assignments = shlex.split(line)
        options = parsed.setdefault(module, {})
        for assignment in assignments:
            key, _, value = assignment.partition("=")
            options[key] = value
    return parsed


def test_package_names_the_gadget_after_the_product():
    """The package must set g_ether's product string to the product name.

    Why this test exists: this string is the connection's name on the host, and
    it is the name Shared mode's instructions tell the user to look for in the
    Internet Sharing list. Left at the kernel default the entry reads "Raspberry
    Pi USB Gadget", which matches nothing the product says.

    Failure: the file is missing or names a different module/option, and the
    host keeps showing the stock name -- visible only on the host, never on the
    board, so nothing on the Pi would report it.
    """
    assert _DROP_IN.is_file(), f"missing {_DROP_IN}"
    options = _module_options(_DROP_IN.read_text(encoding="utf-8"))

    assert set(options) == {GADGET_MODULE}, f"unexpected modules configured: {sorted(options)}"
    assert options[GADGET_MODULE].get("iProduct") == DEVICE_NAME


def test_the_name_survives_shell_style_word_splitting():
    """The value must be quoted in the file, not left bare.

    Why this test exists: the name contains spaces, and kmod splits an options
    line on whitespace. ``iProduct=Universal Chess USB Gadget`` is four options,
    three of which do not exist, so modprobe fails the load and the gadget never
    appears at all -- a broken cable, not a cosmetic difference. The parse above
    is shlex, which would happily accept the bare form as ``iProduct=Universal``
    plus stray words, so the quoting is asserted against the raw text.

    Failure: the quotes are dropped and only the first word reaches the module.
    """
    text = _DROP_IN.read_text(encoding="utf-8")
    assert f'iProduct="{DEVICE_NAME}"' in text


def test_the_usb_ids_are_left_alone():
    """Only the product string may be set, never idVendor/idProduct.

    Why this test exists: the identifiers next to the name in the same parameter
    list belong to Raspberry Pi (0x2e8a/0x0013). Claiming a vendor ID we do not
    hold misidentifies the device to every host, and changing the pair also
    invalidates the driver binding a host has already stored for it. Renaming is
    a string; re-identifying is not ours to do.

    Failure: an idVendor/idProduct/bcdDevice option appears in the drop-in.
    """
    options = _module_options(_DROP_IN.read_text(encoding="utf-8"))[GADGET_MODULE]
    assert set(options) == {"iProduct"}, f"drop-in sets more than the name: {sorted(options)}"


def test_shared_mode_tells_the_user_the_name_the_host_will_show():
    """Shared mode's instructions must name the device exactly as the drop-in does.

    Why this test exists: on macOS the master Internet Sharing switch is not
    enough -- the per-device switch has to be turned off too, or the Mac keeps a
    self-assigned address and nothing reaches the board. Instructions that say
    "this USB gadget device" leave the user guessing which of several entries it
    is. The name is only useful while the two agree, and they are set in different
    files (a modprobe drop-in and the menu catalog), so a rename in one place has
    to fail here rather than on someone's desk.

    Every shipped language is checked: a translation that paraphrases the name
    sends the reader looking for a list entry that does not exist, since the host
    shows the untranslated string.

    Failure: a language's Shared description does not contain the product string
    the drop-in sets.
    """
    translations = _REPO_ROOT / "src" / "universalchess" / "menus" / "catalog" / "translations"
    languages = ["en", *sorted(path.stem for path in translations.glob("*.json"))]
    for language in languages:
        catalog = load_catalog() if language == "en" else get_localized_catalog(language)
        shared = next(
            option
            for option in catalog.raw_menu()["optionSets"]["usb_gadget_mode"]
            if option["value"] == "shared"
        )
        assert DEVICE_NAME in shared["description"], (
            f"[{language}] Shared instructions do not name the device as the host "
            f"shows it ({DEVICE_NAME}): {shared['description']}"
        )


def test_the_drop_in_is_installed_where_modprobe_reads_it():
    """The file must be at ``etc/modprobe.d`` in the package, and only there.

    Why this test exists: modprobe merges ``/lib/modprobe.d`` and
    ``/etc/modprobe.d``, with ``/etc`` winning; a copy shipped under ``lib``
    would lose to any vendor file with an opinion about the same module. A second
    copy anywhere in the payload is worse than either, since which one applies
    depends on lexical order.

    Failure: the file moves out of ``etc/modprobe.d``, or a duplicate appears.
    """
    found = list(_DEB_ROOT.rglob(_DROP_IN.name))
    assert found == [_DROP_IN], f"expected exactly one drop-in at {_DROP_IN}, found {found}"

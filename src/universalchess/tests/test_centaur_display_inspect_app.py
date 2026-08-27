"""Tests for reading display-driver facts from an imported Centaur app tree.

The Original Centaur diagnostics card must report the SPI device, GPIO pin
names/numbers, and panel class from whatever original build was uploaded -- not
from Universal Chess's epdconfig or the translate shim. These tests feed synthetic
app trees (a Nuitka-like ``centaur`` blob plus optional ``.py`` / ``spidev.so``)
into the inspector so a new Centaur version is reflected when its strings change,
and so UC files sitting in the same directory cannot leak into the report.
"""

from __future__ import annotations

import struct
from pathlib import Path

from universalchess.services.centaur_display.inspect_app import (
    inspect_centaur_app_display,
)


def _app_tree(base: Path, *, binary: bytes = b"", py_source: str | None = None) -> Path:
    """A launchable Centaur layout: executable plus the required engines/fonts dirs."""
    app = base / "centaur_home"
    (app / "engines").mkdir(parents=True)
    (app / "fonts").mkdir()
    (app / "centaur").write_bytes(binary)
    (app / "centaur").chmod(0o755)
    if py_source is not None:
        (app / "epaperDef.py").write_text(py_source, encoding="utf-8")
    return app


def test_inspect_reports_not_installed_when_tree_is_absent(tmp_path):
    """Why: the card is on the tab before any import. A missing tree must not
    invent a driver, SPI path, or pin map.

    Failure: installed/scanned stay true or pins/spi are populated from defaults.
    """
    info = inspect_centaur_app_display(tmp_path / "missing")

    assert info["installed"] is False
    assert info["scanned"] is False
    assert info["panel_driver"] is None
    assert info["spi_devices"] == []
    assert info["pins"] == {"rst": None, "dc": None, "busy": None, "cs": None}


def test_inspect_extracts_driver_spi_and_pins_from_uploaded_binary(tmp_path):
    """Why: official builds are Nuitka ELFs; GPIO/SPI facts survive as ASCII
    (class names, ``/dev/spidevN.M``, ``EPAPER_RESET = 12``). The card must
    surface those from the uploaded ``centaur`` file.

    Failure: a blob containing EPaperT5D / spidev1.0 / pin assignments reports
    empty driver, SPI, or pins -- the card would show "not found" for a build
    that does embed them.
    """
    blob = (
        b"padding\0<module epaperT5D>\0EPaperT5D\0epaperT5D.py\0dgt_epaper.py\0"
        b"epaperDef.py\0BCM\0setwarnings\0SpiDev\0spidev\0"
        b"/dev/spidev1.0\0lut_20_vcom\0lut_21_ww_partial\0"
        b"EPAPER_RESET = 12\0EPAPER_DC = 16\0EPAPER_BUSY = 7\0EPAPER_CS = 18\0"
        b"tail"
    )
    app = _app_tree(tmp_path, binary=blob)

    info = inspect_centaur_app_display(app)

    assert info["installed"] is True
    assert info["scanned"] is True
    assert info["panel_driver"] == "EPaperT5D"
    assert "epaperT5D.py" in info["driver_modules"]
    assert "dgt_epaper.py" in info["driver_modules"]
    assert info["controller_family"] == "UC8151D"
    assert info["spi_devices"] == ["/dev/spidev1.0"]
    assert info["spi_library"] == "spidev"
    assert info["gpio_numbering"] == "BCM"
    assert info["pins"] == {"rst": 12, "dc": 16, "busy": 7, "cs": 18}
    assert "EPAPER_RESET" in info["pin_identifiers"]
    assert "EPAPER_DC" in info["pin_identifiers"]
    assert "EPAPER_BUSY" in info["pin_identifiers"]


def test_inspect_reads_pin_assignments_from_python_source_in_the_tree(tmp_path):
    """Why: some original layouts keep ``epaperDef.py`` beside the binary. Pin
    numbers in that source are the uploaded app's wiring and must win over an
    empty compiled blob.

    Failure: a tree whose only pin map is the ``.py`` file reports all-null pins.
    """
    source = (
        "EPAPER_RESET = 12\n"
        "EPAPER_DC = 16\n"
        "EPAPER_BUSY = 7\n"
        "EPAPER_CS = 8\n"
    )
    app = _app_tree(tmp_path, binary=b"EPaperT5D\0", py_source=source)

    info = inspect_centaur_app_display(app)

    assert info["pins"] == {"rst": 12, "dc": 16, "busy": 7, "cs": 8}


def test_inspect_does_not_invent_pin_numbers_when_only_names_are_present(tmp_path):
    """Why: official DGT Nuitka 0.6.5 stores ``EPAPER_RESET`` as a name but the
    BCM integer only as a shared interned small int (the same object as every
    other 12 in the program). There is no ``= 12`` ASCII and no ``mov r0, #12``
    next to a load of the name -- those loads are NameError format strings.
    Filling in 12/16/7/18 from UC's epdconfig would describe the wrong board
    (or the wrong original version).

    Failure: pins become 12/16/7/18 (or any non-null map) when the blob has
    names only.
    """
    blob = b"EPaperT5D\0EPAPER_RESET\0EPAPER_DC\0EPAPER_BUSY\0BCM\0"
    app = _app_tree(tmp_path, binary=blob)

    info = inspect_centaur_app_display(app)

    assert info["pin_identifiers"] == ["EPAPER_BUSY", "EPAPER_DC", "EPAPER_RESET"]
    assert info["pins"] == {"rst": None, "dc": None, "busy": None, "cs": None}


def test_inspect_ignores_uc_shim_and_engine_binaries(tmp_path):
    """Why: ``spishim.so`` is Universal Chess (it contains ``/dev/spidev``) and
    ``engines/`` holds UCI binaries. Scanning either would report UC or engine
    strings as if they came from the original Centaur app.

    Failure: spi_devices includes ``/dev/spidev9.9`` from the shim or
    ``/dev/spidev2.0`` / SSD1680 from the engine.
    """
    app = _app_tree(
        tmp_path,
        binary=b"EPaperT5D\0SpiDev\0/dev/spidev1.0\0",
    )
    (app / "spishim.so").write_bytes(b"UC shim /dev/spidev9.9 PIN_DC=99\n")
    (app / "engines" / "stockfish").write_bytes(
        b"SSD1680 /dev/spidev2.0 EPAPER_RESET = 99\n"
    )

    info = inspect_centaur_app_display(app)

    assert info["spi_devices"] == ["/dev/spidev1.0"]
    assert info["controller_family"] == "UC8151D"
    assert 99 not in info["pins"].values()
    assert "/dev/spidev9.9" not in info["spi_devices"]
    assert "/dev/spidev2.0" not in info["spi_devices"]


def test_inspect_reads_spidev_path_template_from_bundled_spidev_so(tmp_path):
    """Why: the official Nuitka binary calls ``SpiDev.open(bus, device)`` and
    does not embed ``/dev/spidev1.0``; the path format lives in bundled
    ``spidev.so``. That template is still a fact from the uploaded app.

    Failure: a tree whose only SPI string is ``/dev/spidev%d.%d`` in spidev.so
    reports spi_path_template None.
    """
    app = _app_tree(tmp_path, binary=b"EPaperT5D\0SpiDev\0writebytes\0")
    (app / "spidev.so").write_bytes(b"header\0/dev/spidev%d.%d\0Bus or device\0")

    info = inspect_centaur_app_display(app)

    assert info["spi_path_template"] == "/dev/spidev%d.%d"
    assert info["spi_library"] == "spidev"
    assert info["spi_devices"] == []


def test_inspect_detects_ssd1680_family_when_that_is_the_uploaded_driver(tmp_path):
    """Why: a different original version could ship an SSD1680 class instead of
    T5D. The card must follow that upload, not assume every Centaur is UC8151D.

    Failure: an SSD1680-only blob is reported as UC8151D or EPaperT5D.
    """
    app = _app_tree(tmp_path, binary=b"EPaperSSD1680\0SSD1680\0/dev/spidev0.0\0")

    info = inspect_centaur_app_display(app)

    assert info["panel_driver"] == "EPaperSSD1680"
    assert info["controller_family"] == "SSD1680"
    assert info["spi_devices"] == ["/dev/spidev0.0"]


def test_inspect_records_rpi_gpio_backend_from_bundled_extension(tmp_path):
    """Why: Centaur drives RST/DC/BUSY through bundled ``RPi/_GPIO.so``. The
    card should say so when that file is in the upload, not when only UC's
    gpiozero stack is present on the board.

    Failure: a tree with ``RPi/_GPIO.so`` reports gpio_backend None.
    """
    app = _app_tree(tmp_path, binary=b"EPaperT5D\0BCM\0")
    gpio = app / "RPi" / "_GPIO.so"
    gpio.parent.mkdir()
    gpio.write_bytes(b"RPi.GPIO /dev/gpiomem\0")

    info = inspect_centaur_app_display(app)

    assert info["gpio_backend"] == "RPi.GPIO"
    assert info["gpio_numbering"] == "BCM"


def test_inspect_finds_packed_nuitka_module_and_class_names(tmp_path):
    """Why: official Nuitka 0.6 packs identifiers without NULs
    (``epaperT5D.py{EPaperT5D.write_cmd``). Requiring a whole-string match
    would miss the class on the real uploaded binary.

    Failure: a packed blob reports panel_driver None or omits epaperT5D.py.
    """
    app = _app_tree(
        tmp_path,
        binary=b"<module epaperT5D>epaperT5D.py{EPaperT5D.write_cmdBCMsetwarnings",
    )

    info = inspect_centaur_app_display(app)

    assert info["panel_driver"] == "EPaperT5D"
    assert "epaperT5D.py" in info["driver_modules"]
    assert info["gpio_numbering"] == "BCM"


def _minimal_elf32_arm(code_and_data: bytes, *, va_base: int = 0x10000) -> bytes:
    """A stripped ARM ELF32 with one PT_LOAD covering the whole file.

    Official Centaur is this shape (EABI5, little-endian, p_offset 0). The
    inspector recovers BCM immediates from ``mov r0, #N`` / ``ldr ..., [pc]``
    pairs that Nuitka emits for ``EPAPER_RESET = 12`` and the other pin names.
    """
    buf = bytearray(len(code_and_data))
    buf[:] = code_and_data
    buf[0:4] = b"\x7fELF"
    buf[4] = 1  # ELFCLASS32
    buf[5] = 1  # ELFDATA2LSB
    buf[6] = 1
    struct.pack_into("<HHI", buf, 16, 2, 40, 1)  # ET_EXEC, EM_ARM, version
    struct.pack_into("<III", buf, 24, va_base + 0x80, 52, 0)  # entry, phoff, shoff
    struct.pack_into("<IHHHHHH", buf, 36, 0x05000400, 52, 32, 1, 0, 0, 0)
    struct.pack_into(
        "<IIIIIIII",
        buf,
        52,
        1,  # PT_LOAD
        0,
        va_base,
        va_base,
        len(buf),
        len(buf),
        5,  # PF_R|PF_X
        0x10000,
    )
    return bytes(buf)


def _elf_with_epaper_pin_immediates() -> bytes:
    """Nuitka-style A32: ``mov r0, #pin`` then ``ldr r1, [pc, #N]`` of the name.

    Each pin is an isolated 32-byte slot so nearest-backward pairing cannot
    steal a neighbour's immediate -- the same shape as consecutive
    ``EPAPER_* = N`` assignments in epaperDef's module init.
    """
    va_base = 0x10000
    buf = bytearray(0x400)
    names = (
        (b"EPAPER_RESET\x00", 12, 0x100),
        (b"EPAPER_DC\x00", 16, 0x120),
        (b"EPAPER_BUSY\x00", 7, 0x140),
        (b"EPAPER_CS\x00", 18, 0x160),
    )
    str_off = 0x200
    for name, pin, code_off in names:
        buf[str_off : str_off + len(name)] = name
        str_va = va_base + str_off
        # mov r0, #pin ; ldr r1, [pc, #4] ; pool word at +16 = string VA
        struct.pack_into("<I", buf, code_off, 0xE3A00000 | pin)
        struct.pack_into("<I", buf, code_off + 4, 0xE59F1004)
        struct.pack_into("<I", buf, code_off + 16, str_va)
        str_off += 32
    buf[0x300:0x318] = b"EPaperT5D\x00BCM\x00SpiDev\x00"
    return _minimal_elf32_arm(bytes(buf), va_base=va_base)


def test_inspect_extracts_bcm_pin_numbers_from_nuitka_arm_elf(tmp_path):
    """Why: some Nuitka builds compile ``EPAPER_RESET = 12`` to ``mov r0, #12``
    plus a pc-relative load of the name, with no ``= 12`` ASCII. The card must
    still show those BCM numbers from the uploaded ELF.

    Failure: an ELF that encodes RST=12 DC=16 BUSY=7 CS=18 reports all-null
    pins, so the UI falls back to "does not store pin numbers".
    """
    binary = _elf_with_epaper_pin_immediates()
    app = _app_tree(tmp_path, binary=binary)

    info = inspect_centaur_app_display(app)

    assert info["pins"] == {"rst": 12, "dc": 16, "busy": 7, "cs": 18}
    assert "EPAPER_RESET" in info["pin_identifiers"]
    assert "EPAPER_DC" in info["pin_identifiers"]
    assert "EPAPER_BUSY" in info["pin_identifiers"]
    assert "EPAPER_CS" in info["pin_identifiers"]


def test_inspect_extracts_bcm_pins_from_nuitka_interned_pylong(tmp_path):
    """Why: some Nuitka 0.6 builds intern small ints as PyLongObjects and
    ``ldr r0, [pc]`` the object instead of ``mov r0, #12``. Missing that
    form would still leave the official binary's pins blank.

    Failure: an ELF whose pin 12 lives in a PyLong at the ``ldr r0`` target
    reports rst None.
    """
    va_base = 0x10000
    buf = bytearray(0x400)
    name = b"EPAPER_RESET\x00"
    buf[0x200:0x200 + len(name)] = name
    # PyLongObject: refcnt=1, type ptr, ob_size=1, digit=12
    struct.pack_into("<IiI", buf, 0x280 + 4, 0x11111111, 1, 12)
    struct.pack_into("<I", buf, 0x280, 1)
    pylong_va = va_base + 0x280
    name_va = va_base + 0x200
    # ldr r0, [pc, #4] -> pool @ +12 = pylong_va
    # ldr r1, [pc, #4] -> pool @ +16 = name_va  wait need correct pc math.
    # instr 0x100 va 0x10100 pc 0x10108 imm 8 -> 0x10110 file 0x110
    struct.pack_into("<I", buf, 0x100, 0xE59F0008)  # ldr r0, [pc, #8]
    struct.pack_into("<I", buf, 0x104, 0xE59F1008)  # ldr r1, [pc, #8]
    struct.pack_into("<I", buf, 0x110, pylong_va)
    struct.pack_into("<I", buf, 0x114, name_va)
    buf[0x300:0x30A] = b"EPaperT5D\x00"
    app = _app_tree(tmp_path, binary=_minimal_elf32_arm(bytes(buf), va_base=va_base))

    info = inspect_centaur_app_display(app)

    assert info["pins"]["rst"] == 12
    assert info["pins"]["dc"] is None


def test_inspect_packed_busy_reset_strings_do_not_steal_busy_immediate(tmp_path):
    """Why: official DGT packs ``EPAPER_BUSY\\0EPAPER_RESET`` so RESET's
    putative PyObject header (va-12) is BUSY's string. Treating that word as
    RESET would report RST=7 when the only immediate sits next to BUSY.

    Failure: rst is 7 (or both roles null after the uniqueness drop) and
    busy is not 7.
    """
    va_base = 0x10000
    buf = bytearray(0x400)
    packed = b"EPAPER_BUSY\x00EPAPER_RESET\x00"
    buf[0x200 : 0x200 + len(packed)] = packed
    busy_va = va_base + 0x200
    struct.pack_into("<I", buf, 0x140, 0xE3A00000 | 7)
    struct.pack_into("<I", buf, 0x144, 0xE59F1004)
    struct.pack_into("<I", buf, 0x150, busy_va)
    buf[0x300:0x30A] = b"EPaperT5D\x00"
    app = _app_tree(tmp_path, binary=_minimal_elf32_arm(bytes(buf), va_base=va_base))

    info = inspect_centaur_app_display(app)

    assert info["pins"]["busy"] == 7
    assert info["pins"]["rst"] is None
    assert info["pins"]["dc"] is None
    assert info["pins"]["cs"] is None


"""Extract e-paper wiring facts from an imported original Centaur app tree.

The Original Centaur diagnostics card has to describe *that upload* -- GPIO pin
names and numbers, SPI device, panel class -- not Universal Chess's epdconfig
or the translate shim. Official DGT builds are stripped 32-bit Nuitka 0.6.5
ELFs: class names, ``EPAPER_RESET`` identifiers, and ``GPIO.setmode(BCM)``
survive as ASCII. Bundled ``spidev.so`` opens ``/dev/spidev%d.%d``; the
bus/device pair is chosen at runtime and is not a C string in the ELF.

BCM pin *numbers* are a different story. A ``.py`` tree or an unstripped
build may still have ``EPAPER_RESET = 12``. Some Nuitka codegen emits
``mov r0, #12`` immediately before a pc-relative load of that name; those
immediates are recovered. Official DGT Nuitka 0.6.5 does neither: every
load of the ``EPAPER_*`` C strings is a NameError format path, and 7/12/16/18
exist only in the global interned-small-int table (1, 2, 3, … 97) with no
static pairing to the names. Those builds report names without numbers.
They are never filled in from Universal Chess.

Skipped on purpose:
    ``spishim.so`` -- compiled by Universal Chess into CENTAUR_HOME; its
    ``/dev/spidev`` strings are the shim, not the original app.
    ``engines/``, ``fonts/``, ``books/``, ``PIL/``, ``settings/`` -- not the
    panel driver; UCI binaries especially would leak false controller names.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from universalchess.paths import CENTAUR_HOME
from universalchess.services.centaur_import import centaur_app_installed

# 40 MiB covers the official ~17 MiB ``centaur`` ELF with headroom for a
# larger Nuitka one-file build; above that the walk skips the file rather
# than reading an engine or media blob that snuck past the directory filter.
_MAX_FILE_BYTES = 40 * 1024 * 1024

_SKIP_DIR_NAMES = frozenset({"engines", "fonts", "books", "PIL", "settings"})
_SKIP_FILE_NAMES = frozenset({"spishim.so"})

_SPIDEV_PATH = re.compile(rb"/dev/spidev(\d+)\.(\d+)")
_SPIDEV_TEMPLATE = re.compile(rb"/dev/spidev%d\.%d")
_PIN_ASSIGN = re.compile(
    rb"(EPAPER_RESET|EPAPER_DC|EPAPER_BUSY|EPAPER_CS|"
    rb"RST_PIN|DC_PIN|BUSY_PIN|CS_PIN|"
    rb"reset_pin|dc_pin|busy_pin|cs_pin)"
    rb"\s*=\s*(\d{1,2})"
)
_PIN_NAME = re.compile(
    rb"\b(EPAPER_RESET|EPAPER_DC|EPAPER_BUSY|EPAPER_CS|"
    rb"RST_PIN|DC_PIN|BUSY_PIN|CS_PIN)\b"
)

_PIN_ROLES = {
    "EPAPER_RESET": "rst",
    "RST_PIN": "rst",
    "reset_pin": "rst",
    "EPAPER_DC": "dc",
    "DC_PIN": "dc",
    "dc_pin": "dc",
    "EPAPER_BUSY": "busy",
    "BUSY_PIN": "busy",
    "busy_pin": "busy",
    "EPAPER_CS": "cs",
    "CS_PIN": "cs",
    "cs_pin": "cs",
}

# Panel class tokens found as whole ASCII strings in Nuitka constant tables.
_DRIVER_CLASS_ORDER = (
    "EPaperT5D",
    "EPaperSSD1680",
    "EPaperIL3820",
    "EPaperUC8151D",
)

_DRIVER_TO_FAMILY = {
    "EPaperT5D": "UC8151D",
    "EPaperUC8151D": "UC8151D",
    "EPaperSSD1680": "SSD1680",
    "EPaperIL3820": "IL3820",
}

_MODULE_NAMES = (
    "epaperT5D.py",
    "dgt_epaper.py",
    "epaperDef.py",
    "epaper.py",
)

# UC8151D waveform LUT names used by EPaperT5D; a LUT load is not an SSD1680
# RAM write, so these names identify the T5D family even when the class string
# is missing from a particular build.
_UC8151D_LUT_MARKERS = (
    b"lut_20_vcom",
    b"lut_21_ww",
    b"lut_22_bw",
    b"lut_23_wb",
    b"lut_24_bb",
)

_EMPTY_PINS = {"rst": None, "dc": None, "busy": None, "cs": None}


def inspect_centaur_app_display(app_dir: os.PathLike | str | None = None) -> dict:
    """Return display-driver facts extracted from an imported Centaur tree.

    Args:
        app_dir: Directory that holds the imported app (the ``centaur``
            executable). Defaults to ``CENTAUR_HOME``. Injected in tests.

    Returns:
        A JSON-serializable dict. ``installed`` is the launchable-app check
        (executable plus engines and fonts). ``scanned`` is true only when at
        least one eligible file was read. Pin numbers come from readable
        ``NAME = N`` assignments when present, otherwise from ARM immediates
        next to those names in a Nuitka ELF. Official DGT 0.6.5 has neither
        pairing, so those pins stay null. They are never filled in from
        Universal Chess's epdconfig.
    """
    root = Path(app_dir) if app_dir is not None else Path(CENTAUR_HOME)
    installed = centaur_app_installed(str(root))
    if not root.is_dir():
        return _public(_empty(installed=installed, scanned=False))

    files = list(_iter_scan_files(root))
    if not files:
        return _public(_empty(installed=installed, scanned=False))

    merged = _empty(installed=installed, scanned=True)
    pin_values: Dict[str, Optional[int]] = dict(_EMPTY_PINS)
    pin_conflict: Set[str] = set()

    for path in files:
        chunk = _read_capped(path)
        if chunk is None:
            continue
        _merge_file(merged, pin_values, pin_conflict, chunk, path)

    # Nuitka sometimes compiles ``EPAPER_RESET = 12`` to ``mov r0, #12`` plus a
    # pc-relative load of the name. Fill roles that ASCII assignments did not
    # already set. Official DGT 0.6.5 has no such pairing (see module docstring).
    centaur_path = root / "centaur"
    if centaur_path.is_file():
        blob = _read_capped(centaur_path)
        if blob is not None:
            for role, number in _extract_elf_arm_pins(blob).items():
                if role in pin_conflict:
                    continue
                existing = pin_values.get(role)
                if existing is None:
                    pin_values[role] = number
                elif existing != number:
                    pin_conflict.add(role)
                    pin_values[role] = None

    for role in pin_conflict:
        pin_values[role] = None
    merged["pins"] = pin_values
    merged["driver_modules"] = sorted(set(merged["driver_modules"]))
    merged["pin_identifiers"] = sorted(set(merged["pin_identifiers"]))
    merged["spi_devices"] = sorted(set(merged["spi_devices"]))
    merged["panel_driver"] = _pick_driver(merged.pop("panel_driver_candidates"))
    merged["controller_family"] = _controller_family(
        panel_driver=merged["panel_driver"],
        lut_uc8151d=bool(merged.pop("_lut_flag")),
        token_uc8151d=bool(merged.pop("_uc_flag")),
        token_ssd1680=bool(merged.pop("_ssd_flag")),
        token_il3820=bool(merged.pop("_il_flag")),
    )
    return _public(merged)


def _public(payload: dict) -> dict:
    """Drop inspector-only keys so the HTTP payload is only user-facing facts."""
    for key in (
        "panel_driver_candidates",
        "_lut_flag",
        "_uc_flag",
        "_ssd_flag",
        "_il_flag",
    ):
        payload.pop(key, None)
    return payload


def _empty(*, installed: bool, scanned: bool) -> dict:
    return {
        "installed": installed,
        "scanned": scanned,
        "panel_driver": None,
        "panel_driver_candidates": [],
        "driver_modules": [],
        "controller_family": None,
        "spi_devices": [],
        "spi_path_template": None,
        "spi_library": None,
        "gpio_numbering": None,
        "gpio_backend": None,
        "pin_identifiers": [],
        "pins": dict(_EMPTY_PINS),
        "_lut_flag": False,
        "_uc_flag": False,
        "_ssd_flag": False,
        "_il_flag": False,
    }


def _iter_scan_files(root: Path) -> Iterable[Path]:
    """Yield original-app files that can carry panel-driver strings."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            if name in _SKIP_FILE_NAMES:
                continue
            path = Path(dirpath) / name
            if _should_scan(path, root):
                yield path


def _should_scan(path: Path, root: Path) -> bool:
    if path.name == "centaur" and path.parent == root:
        return True
    if path.suffix == ".py":
        return True
    if path.name == "spidev.so":
        return True
    if path.name == "_GPIO.so":
        return True
    if "epaper" in path.name.lower():
        return True
    return False


def _read_capped(path: Path) -> Optional[bytes]:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _MAX_FILE_BYTES:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _merge_file(
    merged: dict,
    pin_values: Dict[str, Optional[int]],
    pin_conflict: Set[str],
    data: bytes,
    path: Path,
) -> None:
    for cls in _DRIVER_CLASS_ORDER:
        if cls.encode("ascii") in data:
            merged["panel_driver_candidates"].append(cls)

    for module in _MODULE_NAMES:
        if module.encode("ascii") in data or path.name == module:
            merged["driver_modules"].append(module)

    if path.name == "spidev.so" or b"spidev" in data or b"SpiDev" in data:
        merged["spi_library"] = "spidev"

    if _SPIDEV_TEMPLATE.search(data):
        merged["spi_path_template"] = "/dev/spidev%d.%d"

    for match in _SPIDEV_PATH.finditer(data):
        merged["spi_devices"].append(match.group().decode("ascii"))

    # Official Nuitka packs ``BCM`` + ``setwarnings`` without a NUL
    # (``<module dgt_epaper>BCMsetwarningsdgt_epaper.py``). Either form is
    # GPIO.setmode(BCM) in this app.
    if b"BCM" in data and (
        b"setwarnings" in data or b"setmode" in data or path.name == "centaur"
    ):
        merged["gpio_numbering"] = "BCM"

    if path.name == "_GPIO.so" or b"RPi.GPIO" in data:
        merged["gpio_backend"] = "RPi.GPIO"

    if any(marker in data for marker in _UC8151D_LUT_MARKERS):
        merged["_lut_flag"] = True

    if b"SSD1680" in data:
        merged["_ssd_flag"] = True
    if b"IL3820" in data:
        merged["_il_flag"] = True
    if b"UC8151D" in data:
        merged["_uc_flag"] = True

    for match in _PIN_NAME.finditer(data):
        merged["pin_identifiers"].append(match.group().decode("ascii"))

    for match in _PIN_ASSIGN.finditer(data):
        name = match.group(1).decode("ascii")
        value = int(match.group(2))
        role = _PIN_ROLES.get(name)
        if role is None:
            continue
        if role in pin_conflict:
            continue
        existing = pin_values.get(role)
        if existing is None:
            pin_values[role] = value
        elif existing != value:
            pin_conflict.add(role)
            pin_values[role] = None


def _pick_driver(candidates: List[str]) -> Optional[str]:
    for cls in _DRIVER_CLASS_ORDER:
        if cls in candidates:
            return cls
    return None


def _controller_family(
    *,
    panel_driver: Optional[str],
    lut_uc8151d: bool,
    token_uc8151d: bool,
    token_ssd1680: bool,
    token_il3820: bool,
) -> Optional[str]:
    if panel_driver in _DRIVER_TO_FAMILY:
        return _DRIVER_TO_FAMILY[panel_driver]
    if lut_uc8151d or token_uc8151d:
        return "UC8151D"
    if token_ssd1680:
        return "SSD1680"
    if token_il3820:
        return "IL3820"
    return None


# ARM ELF recovery for Nuitka builds that still emit ``mov r0, #12``
# immediately before a pc-relative ``ldr`` of the pin name (or of a
# PyObject wrapping it). Official DGT 0.6.5 does not use that shape.
_EM_ARM = 40
_PT_LOAD = 1
_PF_X = 1
_BCM_PIN_MIN = 2
_BCM_PIN_MAX = 27
_MOV_LOOKBACK = 128
_ARM_PIN_NAMES = (
    (b"EPAPER_RESET", "rst"),
    (b"RST_PIN", "rst"),
    (b"EPAPER_DC", "dc"),
    (b"DC_PIN", "dc"),
    (b"EPAPER_BUSY", "busy"),
    (b"BUSY_PIN", "busy"),
    (b"EPAPER_CS", "cs"),
    (b"CS_PIN", "cs"),
)


def _extract_elf_arm_pins(data: bytes) -> Dict[str, int]:
    """Return BCM pin roles recovered from A32 immediates in an ARM ELF32.

    A non-ELF blob (the ASCII fixtures) returns empty so names without
    immediates stay unnumbered.
    """
    loads = _elf32_le_arm_loads(data)
    if not loads:
        return {}
    name_vas: Dict[int, str] = {}
    string_vas = set()
    for name, role in _ARM_PIN_NAMES:
        for va in _nul_term_vas(data, loads, name):
            name_vas[va] = role
            string_vas.add(va)
    # CPython/Nuitka PyObject headers sit 8 or 12 bytes before some
    # interned strings. Official DGT packs BUSY immediately before RESET,
    # so RESET's va-12 *is* BUSY's string; claiming that word as RESET
    # would attribute BUSY's ldr to the wrong role.
    for name, role in _ARM_PIN_NAMES:
        for va in _nul_term_vas(data, loads, name):
            for header_va in (va - 8, va - 12):
                if header_va not in string_vas:
                    name_vas[header_va] = role
    if not name_vas:
        return {}
    role_imms: Dict[str, Set[int]] = {}
    for ldr_off, loaded in _iter_ldr_pc_values(data, loads):
        role = name_vas.get(loaded)
        if role is None:
            continue
        imm = _lookback_bcm_value(data, loads, ldr_off)
        if imm is None:
            continue
        role_imms.setdefault(role, set()).add(imm)
    numbered = {role: next(iter(imms)) for role, imms in role_imms.items() if len(imms) == 1}
    counts: Dict[int, int] = {}
    for number in numbered.values():
        counts[number] = counts.get(number, 0) + 1
    return {role: number for role, number in numbered.items() if counts[number] == 1}


def _elf32_le_arm_loads(data: bytes) -> List[Tuple[int, int, int, int]]:
    if len(data) < 52:
        return []
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        return []
    if struct.unpack_from("<H", data, 18)[0] != _EM_ARM:
        return []
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phentsize = struct.unpack_from("<H", data, 42)[0]
    e_phnum = struct.unpack_from("<H", data, 44)[0]
    if e_phentsize < 32:
        return []
    loads: List[Tuple[int, int, int, int]] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 32 > len(data):
            break
        p_type, p_offset, p_vaddr, _paddr, p_filesz, _memsz, p_flags, _align = struct.unpack_from(
            "<IIIIIIII", data, off
        )
        if p_type == _PT_LOAD and p_filesz:
            loads.append((p_offset, p_vaddr, p_filesz, p_flags))
    return loads


_Load = Tuple[int, int, int, int]


def _va_to_off(loads: List[_Load], va: int) -> Optional[int]:
    for file_off, vbase, filesz, _flags in loads:
        if vbase <= va < vbase + filesz:
            return file_off + (va - vbase)
    return None


def _off_to_va(loads: List[_Load], file_off: int) -> Optional[int]:
    for load_off, vbase, filesz, _flags in loads:
        if load_off <= file_off < load_off + filesz:
            return vbase + (file_off - load_off)
    return None


def _nul_term_vas(data: bytes, loads: List[_Load], name: bytes) -> List[int]:
    vas: List[int] = []
    needle = name + b"\x00"
    start = 0
    while True:
        hit = data.find(needle, start)
        if hit < 0:
            break
        va = _off_to_va(loads, hit)
        if va is not None:
            vas.append(va)
        start = hit + 1
    return vas


def _iter_ldr_pc_values(data: bytes, loads: List[_Load]) -> Iterable[Tuple[int, int]]:
    """Yield (ldr_file_offset, loaded_word) for A32 ``ldr rd, [pc, #+/-imm]``."""
    for file_off, vbase, filesz, flags in loads:
        if not (flags & _PF_X):
            continue
        end = min(len(data), file_off + filesz)
        off = (file_off + 3) & ~3
        while off + 4 <= end:
            word = struct.unpack_from("<I", data, off)[0]
            top = word >> 16
            instr_va = vbase + (off - file_off)
            if top == 0xE59F:
                target_va = (instr_va + 8 + (word & 0xFFF)) & ~3
            elif top == 0xE51F:
                target_va = (instr_va + 8 - (word & 0xFFF)) & ~3
            else:
                off += 4
                continue
            target_off = _va_to_off(loads, target_va)
            if target_off is not None and target_off + 4 <= len(data):
                yield off, struct.unpack_from("<I", data, target_off)[0]
            off += 4


def _a32_mov_r0_imm(word: int) -> Optional[int]:
    # mov r0, #imm8 (rotate 0) -- Nuitka uses this for BCM pin numbers < 256.
    if (word & 0xFFFFF000) == 0xE3A00000:
        return word & 0xFF
    # movw r0, #imm16 (ARMv7); imm4 is 0 for pins 2-27 so the mask is exact.
    if (word & 0xFF0FF000) == 0xE3000000:
        return ((word >> 16) & 0xF) << 12 | (word & 0xFFF)
    return None


def _pylong_digit(data: bytes, loads: List[_Load], obj_va: int) -> Optional[int]:
    """Read a CPython 3.5 32-bit PyLongObject's single digit (Nuitka interned ints)."""
    off = _va_to_off(loads, obj_va)
    if off is None or off + 16 > len(data):
        return None
    ob_size = struct.unpack_from("<i", data, off + 8)[0]
    if ob_size != 1:
        return None
    digit = struct.unpack_from("<I", data, off + 12)[0]
    if _BCM_PIN_MIN <= digit <= _BCM_PIN_MAX:
        return digit
    return None


def _pylong_imm_from_ldr(data: bytes, loads: List[_Load], off: int) -> Optional[int]:
    """``ldr r0, [pc, #imm]`` of a pointer to an interned PyLong pin constant."""
    word = struct.unpack_from("<I", data, off)[0]
    if (word >> 16) != 0xE59F or ((word >> 12) & 0xF) != 0:
        return None
    instr_va = _off_to_va(loads, off)
    if instr_va is None:
        return None
    target_va = (instr_va + 8 + (word & 0xFFF)) & ~3
    ptr_off = _va_to_off(loads, target_va)
    if ptr_off is None or ptr_off + 4 > len(data):
        return None
    obj_va = struct.unpack_from("<I", data, ptr_off)[0]
    return _pylong_digit(data, loads, obj_va)


def _lookback_bcm_value(data: bytes, loads: List[_Load], ldr_off: int) -> Optional[int]:
    start = max(0, ldr_off - _MOV_LOOKBACK) & ~3
    best: Optional[Tuple[int, int]] = None
    for off in range(start, ldr_off, 4):
        word = struct.unpack_from("<I", data, off)[0]
        imm = _a32_mov_r0_imm(word)
        if imm is None:
            imm = _pylong_imm_from_ldr(data, loads, off)
        if imm is None or not (_BCM_PIN_MIN <= imm <= _BCM_PIN_MAX):
            continue
        dist = ldr_off - off
        if best is None or dist < best[0]:
            best = (dist, imm)
        elif dist == best[0] and imm != best[1]:
            return None
    return None if best is None else best[1]

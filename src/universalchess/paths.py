"""Centralized path constants, resource resolution, and FEN operations for DGTCentaurMods.

This module defines all base paths used throughout the application.
All paths are absolute and should be used via import.

FEN operations are here because they only read/write files and don't need
hardware access. This allows the web app to use them without importing board.

Database URI resolution is in db/uri.py
E-paper image writing is in services/chromecast.py
"""

# This file is part of the DGTCentaur Mods open source software
# ( https://github.com/EdNekebno/DGTCentaur )
#
# DGTCentaur Mods is free software: you can redistribute
# it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# DGTCentaur Mods is distributed in the hope that it will
# be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this file.  If not, see
#
# https://github.com/EdNekebno/DGTCentaur/blob/master/LICENSE.md
#
# This and any other notices must remain intact and unaltered in any
# distribution, modification, variant, or derivative of this software.

import os
from pathlib import Path

# Base installation directory
BASE_DIR = "/opt/universalchess"

# Subdirectories under BASE_DIR
DB_DIR = f"{BASE_DIR}/db"
CONFIG_DIR = f"{BASE_DIR}/config"
ENGINES_DIR = f"{BASE_DIR}/engines"
TMP_DIR = f"{BASE_DIR}/tmp"  # noqa: S108  # nosec B108 - app subdir under BASE_DIR, not world-writable /tmp
WEB_DIR = f"{BASE_DIR}/web"
WEB_STATIC_DIR = f"{WEB_DIR}/static"
SCRIPTS_DIR = f"{BASE_DIR}/scripts"

# Pinned root helpers invoked via a single passwordless sudo grant each (the
# postinst installs the matching /etc/sudoers.d drop-ins). Granting NOPASSWD on
# one fixed helper -- which performs exactly the allowed operations and refuses
# anything else -- is least-privilege compared with granting it on the
# general-purpose tools (btmgmt/rfkill) the helper wraps. Shared here so the
# callers and the sudoers entries reference one path.
#
# bt-admin: configures the controller for connectable LE advertising (btmgmt)
# and toggles the radio (rfkill). Without its grant the controller is never
# configured and phone chess apps cannot discover the board over BLE.
BT_ADMIN = f"{SCRIPTS_DIR}/bt-admin"

# Resources directory relative to this file (works in both installed and dev environments)
# This file is at: <base>/paths.py, so resources is at: <base>/resources
RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")

# User-customizable resources (takes precedence over system resources).
# Resolved at import time from the running user's home directory.
_HOME = str(Path.home())
USER_RESOURCES_DIR = os.path.join(_HOME, "resources")

# Specific files
FEN_LOG = f"{TMP_DIR}/fen.log"
EPAPER_STATIC_JPG = f"{WEB_STATIC_DIR}/epaper.jpg"
DEFAULT_DB_FILE = f"{DB_DIR}/centaur.db"

# Managed install location for the original DGT Centaur software. This is the
# single canonical directory the SD import writes to and that the launcher,
# the web availability check, and the display shim all read from. Centralizing
# it here removes the previous dependence on ad-hoc paths (/home/pi/centaur and
# /opt/DGTCentaurMods) that had to be created by hand.
CENTAUR_HOME = os.path.join(_HOME, "centaur")

# Original DGT Centaur software executable. Shared so the board (which launches
# it) and the web UI (which only offers the action when it exists) agree on the
# path without the web process importing board/hardware modules.
CENTAUR_SOFTWARE = os.path.join(CENTAUR_HOME, "centaur")

# LD_PRELOAD shim for centaur "translate" mode: it virtualizes centaur's panel
# (fakes the BUSY handshake, absorbs its SPI/GPIO) and forwards the DC-tagged SPI
# stream to UC's display gateway, so centaur renders on whatever panel is fitted.
# Shipped alongside the centaur binary.
CENTAUR_DISPLAY_SHIM = os.path.join(CENTAUR_HOME, "spishim.so")


def get_resource_path(resource_file: str) -> str:
    """Return resource path from the user resources folder or system resources.
    
    Checks user resources directory first, then falls back to system resources.
    Rejects paths containing '..' for security.
    
    Args:
        resource_file: Name of the resource file (e.g., "Font.ttc")
        
    Returns:
        Absolute path to the resource file
    """
    if ".." in resource_file:
        return ""

    user_path = os.path.join(USER_RESOURCES_DIR, resource_file)
    if os.path.exists(user_path):
        return user_path
    return os.path.join(RESOURCES_DIR, resource_file)


def get_engine_path(engine_name: str) -> str:
    """Return path to a UCI engine executable.
    
    Checks installed location first (/opt/universalchess/engines),
    then falls back to development location (relative to this file).
    
    Args:
        engine_name: Name of the engine executable (e.g., "stockfish", "ct800")
        
    Returns:
        Absolute path to the engine executable, or empty string if not found
    """
    # engine_name is request-derived (a selected/custom engine id), so it is
    # never used to build a path directly. Each trusted directory is enumerated
    # and the name is only matched against real entries; the returned path is
    # built from the os.listdir entry (a filesystem-sourced value), so no
    # untrusted data flows into a path expression (CWE-22). A non-recursive
    # listing also means a traversing name (with "/" or "..") never matches a
    # top-level entry, so it is rejected implicitly.
    #
    # Directory order: installed location first, then the development location
    # relative to this file. os.path.exists still follows a leaf symlink, so a
    # system engine installed as <engines>/<name> -> /usr/games/<name> (see
    # engine_manager._install_system_package) resolves correctly.
    dev_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines")
    if not engine_name:
        return ""

    # Custom-script engines (e.g. Maia) install their executable inside a
    # subdirectory named after the engine (engines/maia/lc0 + weights) rather
    # than as a single top-level file. The relative binary path is a trusted
    # catalog constant (never request-derived), so descending with it does not
    # widen the traversal surface. Imported lazily because engine_manager imports
    # this module -- a top-level import would be circular -- and to keep this
    # low-level module importable without the manager's dependencies present.
    try:
        from universalchess.managers.engine_manager import engine_binary_subpath
    except ImportError:
        # The catalog is an enhancement to resolution, not a prerequisite: if it
        # cannot be imported, fall back to the plain top-level name match below.
        subpath = None
    else:
        subpath = engine_binary_subpath(engine_name)

    for directory in (ENGINES_DIR, dev_dir):
        # A missing directory is normal (the dev engines dir is absent on an
        # installed system, and ENGINES_DIR is absent in a dev checkout), so it
        # is skipped rather than treated as an error. os.listdir on an existing,
        # readable directory does not raise in normal operation; a permission
        # error there is a real fault and is allowed to surface.
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            if entry == engine_name:
                candidate = os.path.join(directory, entry)
                if subpath:
                    # The matched entry is the install subdirectory; the real
                    # executable is inside it. Only return it when it actually
                    # exists so a partial/interrupted install (directory present,
                    # binary missing) reports "not installed" rather than handing
                    # a directory to the engine launcher.
                    binary = os.path.join(candidate, subpath)
                    if os.path.exists(binary):
                        return binary
                elif os.path.exists(candidate):
                    return candidate

    return ""


# -----------------------------------------------------------------------------
# FEN Log Operations
# -----------------------------------------------------------------------------
# These functions manage the FEN log file used for external display (Chromecast, web).
# They are here (not in managers/game.py) because they only do file I/O and don't
# need hardware access. This allows the web app to import them without triggering
# board initialization.

DEFAULT_START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def get_fen_log_path() -> str:
    """Return the fen.log path."""
    return FEN_LOG


def write_fen_log(fen: str) -> None:
    """Write FEN to fen.log for external consumers (Chromecast, web).
    
    Args:
        fen: FEN string to write
    """
    import os
    os.makedirs(os.path.dirname(FEN_LOG), exist_ok=True)
    with open(FEN_LOG, "w", encoding="utf-8") as f:
        f.write(fen)


def get_current_fen() -> str:
    """Return the current FEN from fen.log.
    
    Behavior:
    - If fen.log exists and has content, return its first line as-is.
    - If fen.log is missing, return the starting FEN.
    - If fen.log is empty, return the starting FEN.
    
    Returns:
        Current FEN string or default starting position
    """
    try:
        with open(FEN_LOG, "r", encoding="utf-8") as f:
            curfen = f.readline().strip()
    except FileNotFoundError:
        return DEFAULT_START_FEN
    return curfen or DEFAULT_START_FEN


def get_current_placement() -> str:
    """Return only the board placement part of the current FEN.
    
    Returns:
        Board placement string (first part of FEN before the space)
    """
    fen = get_current_fen()
    return fen.split()[0] if fen else ""


def get_current_turn() -> str:
    """Return the current turn from the FEN ('w' or 'b').
    
    Returns:
        'w' for white's turn, 'b' for black's turn
    """
    fen = get_current_fen()
    parts = fen.split()
    return parts[1] if len(parts) > 1 else "w"


def get_current_castling() -> str:
    """Return the castling rights from the current FEN.
    
    Returns:
        Castling rights string (e.g., 'KQkq', '-')
    """
    fen = get_current_fen()
    parts = fen.split()
    return parts[2] if len(parts) > 2 else "-"


def get_current_en_passant() -> str:
    """Return the en passant square from the current FEN.
    
    Returns:
        En passant square (e.g., 'e3') or '-' if none
    """
    fen = get_current_fen()
    parts = fen.split()
    return parts[3] if len(parts) > 3 else "-"


def get_current_halfmove_clock() -> int:
    """Return the halfmove clock from the current FEN.
    
    Returns:
        Number of halfmoves since last capture or pawn move
    """
    fen = get_current_fen()
    parts = fen.split()
    try:
        return int(parts[4]) if len(parts) > 4 else 0
    except ValueError:
        return 0

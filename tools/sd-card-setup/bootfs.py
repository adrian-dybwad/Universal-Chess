"""Pure transformations for a Raspberry Pi SD-card boot partition.

Every function here maps text to text and never touches the filesystem, so the
logic that decides what a boot partition should contain is testable without an
SD card. All filesystem access, device discovery and user confirmation lives in
``enable_usb_gadget.py``.

Why these particular edits
--------------------------
Reaching a headless Pi Zero over a USB cable needs three things to be true
before the first boot, and only the FAT boot partition is writable from a
Windows or macOS host (the root filesystem is ext4):

1. The dwc2 controller must run in peripheral mode -- a ``config.txt`` overlay.
2. ``dwc2`` and ``g_ether`` must load at boot -- a ``cmdline.txt`` parameter.
   Doing this from the kernel command line rather than ``/etc/modules-load.d``
   means the gadget is live on the *first* boot, with no reboot and no
   dependency on userspace having run yet.
3. ``usb0`` must get an address and hand one to the host -- a NetworkManager
   profile, which lives on the root filesystem and so must be created on the
   device. That is delegated to Raspberry Pi's ``rpi-usb-gadget`` script via a
   cloud-init ``runcmd``.

Step 3 deliberately avoids cloud-init's own ``rpi: enable_usb_gadget: true``
key. That path runs the same script under a 15-second timeout
(``cc_raspberry_pi.configure_usb_gadget``), and the script takes roughly 19
seconds on a Pi Zero 2 W because of the ``nmcli`` settle waits it performs. The
timeout expires, the module reports failure and skips its reboot, and gadget
mode never comes up -- on precisely the hardware this tool targets.
``runcmd`` carries no such timeout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

# Byte-identical to the line rpi-usb-gadget writes. That script de-duplicates by
# deleting lines exactly matching its own literal before appending, so any
# deviation here would leave two conflicting dwc2 overlays in config.txt.
DWC2_OVERLAY_LINE = "dtoverlay=dwc2,dr_mode=peripheral"

GADGET_MODULES = ("dwc2", "g_ether")

# Guarded so the vendor script's de-duplication still recognises the overlay
# line, while the comment tells a human where it came from.
CONFIG_MARKER = "# Universal Chess: USB Ethernet gadget"

CLOUD_CONFIG_HEADER = "#cloud-config"

# `-f` skips the script's interactive "unsupported device?" prompt. Without it
# the script blocks on a read from a closed stdin and spins forever.
GADGET_RUNCMD = "command -v rpi-usb-gadget >/dev/null 2>&1 && rpi-usb-gadget on -f || true"

CLIENT_MODE = "client"
SHARED_MODE = "shared"
# Neither profile pinned: the vendor's watcher picks between them, and goes on
# picking for as long as the board runs.
AUTO_MODE = "auto"
MODES = (CLIENT_MODE, SHARED_MODE, AUTO_MODE)

# The NetworkManager profiles rpi-usb-gadget creates, named as it names them.
CLIENT_CONN = "USB Gadget (client)"
SHARED_CONN = "USB Gadget (shared)"

_PROFILES = {CLIENT_MODE: CLIENT_CONN, SHARED_MODE: SHARED_CONN}

# The vendor's mode watcher, which describes itself as "USB gadget ICS
# auto-switcher (client <-> shared)". It moves the gadget between the two
# profiles according to whether the host looks like it is offering Internet
# Sharing, so a mode is only pinned once this is off -- and Auto mode is this
# unit left running.
ICS_UNIT = "rpi-usb-gadget-ics.service"

# The Pi's own address in Shared mode, where it runs the DHCP server.
GADGET_ADDRESS = "10.12.194.1"

# A cloud-config top-level key starts at column zero. Block scalars and nested
# mappings are always indented, so a column-zero match cannot be inside one.
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.*)$")
_SEQUENCE_ITEM = re.compile(r"^(\s*)-\s")

_DEFAULT_ITEM_INDENT = "  "

_BOOT_REQUIRED_FILES = ("config.txt", "cmdline.txt")
_BOOT_FIRMWARE_MARKERS = ("start.elf", "start4.elf", "kernel8.img", "overlays")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ensure_trailing_newline(text: str) -> str:
    """Return ``text`` terminated by exactly one newline."""
    return text.rstrip("\n") + "\n" if text.strip() else text


def _single_cmdline(cmdline_txt: str) -> str:
    """Return the sole content line of a ``cmdline.txt``.

    Raises:
        ValueError: If the file is blank, or holds more than one non-blank
            line. Both mean the caller is not looking at a usable cmdline.txt,
            and editing it anyway would yield a kernel command line missing
            ``root=`` -- an unbootable card with no console diagnostic.

    """
    content_lines = [line for line in cmdline_txt.splitlines() if line.strip()]
    if not content_lines:
        message = "cmdline.txt is empty; refusing to construct one from scratch"
        raise ValueError(message)
    if len(content_lines) > 1:
        message = (
            "cmdline.txt must be a single line; found "
            f"{len(content_lines)}. Refusing to edit a file that is already malformed."
        )
        raise ValueError(message)
    return content_lines[0].strip()


# ---------------------------------------------------------------------------
# config.txt
# ---------------------------------------------------------------------------


def enable_dwc2_overlay(config_txt: str) -> str:
    """Return ``config_txt`` with the dwc2 peripheral-mode overlay enabled.

    The overlay is emitted under an explicit ``[all]`` filter. config.txt is
    processed as a sequence of conditional sections (``[cm4]``, ``[pi5]``,
    ``[all]``), and a line appended while a model filter is still in effect
    applies only to that model. Stock images happen to end in ``[all]``, but a
    user or another tool may have appended a section since, so the scope is
    restated rather than assumed.

    Idempotent: returns the input unchanged when the overlay is already present.
    """
    if DWC2_OVERLAY_LINE in (line.strip() for line in config_txt.splitlines()):
        return _ensure_trailing_newline(config_txt)

    body = _ensure_trailing_newline(config_txt)
    return f"{body}{CONFIG_MARKER}\n[all]\n{DWC2_OVERLAY_LINE}\n"


# ---------------------------------------------------------------------------
# cmdline.txt
# ---------------------------------------------------------------------------


def add_modules_load(cmdline_txt: str, modules: Sequence[str]) -> str:
    """Return ``cmdline_txt`` with ``modules`` present in ``modules-load=``.

    An existing ``modules-load=`` token is extended in place rather than having a
    second one appended: which occurrence of a repeated parameter wins is up to
    whoever reads it -- here systemd-modules-load, not the kernel -- and a card
    must not depend on that. One token is unambiguous. Existing modules keep
    their position because load order matters for dependent modules.

    ``src/universalchess/scripts/uc-usb-gadget-files.py`` does the same edit on
    the board itself, and the two are held to the same result by
    ``test_the_board_and_the_card_arm_the_same_command_line``.

    Idempotent: modules already listed are not added again.

    Raises:
        ValueError: If the file is blank or holds more than one line.

    """
    tokens = _single_cmdline(cmdline_txt).split()

    index = next(
        (i for i, token in enumerate(tokens) if token.startswith("modules-load=")),
        None,
    )
    if index is None:
        tokens.append("modules-load=" + ",".join(modules))
    else:
        values = [v for v in tokens[index][len("modules-load=") :].split(",") if v]
        values.extend(m for m in modules if m not in values)
        tokens[index] = "modules-load=" + ",".join(values)

    return " ".join(tokens) + "\n"


def remove_serial_console(cmdline_txt: str) -> str:
    """Return ``cmdline_txt`` with any ``console=serial*`` parameter removed.

    Only the serial console is dropped; ``console=tty1`` is kept so a boot
    failure remains diagnosable on a monitor. Matching by parameter name rather
    than by position matters -- a positional strip removes ``root=`` on a
    cmdline that has already been processed, producing an unbootable card.

    Idempotent, and a no-op when no serial console is configured.
    """
    tokens = [
        token
        for token in _single_cmdline(cmdline_txt).split()
        if not token.startswith("console=serial")
    ]
    return " ".join(tokens) + "\n"


# ---------------------------------------------------------------------------
# The cloud-init user-data document
# ---------------------------------------------------------------------------


def parse_cloud_config(text: str) -> dict:
    """Parse a cloud-config document into a mapping.

    An empty or comment-only document yields an empty mapping, matching
    cloud-init's own treatment of a file with nothing to act on.

    Raises:
        RuntimeError: If PyYAML is unavailable.
        yaml.YAMLError: If the document is malformed.

    """
    # Function-local by design: the single-file build normally runs under a
    # system Python with no PyYAML, so a top-level import would stop the tool
    # from starting at all instead of degrading to the unvalidated path.
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover -- environment-dependent
        message = (
            "PyYAML is required to validate user-data. Install it with: "
            "python3 -m pip install pyyaml"
        )
        raise RuntimeError(message) from exc

    parsed = yaml.safe_load(text)
    return {} if parsed is None else parsed


def scan_cloud_config_identity(text: str) -> tuple[str | None, str | None]:
    """Read the hostname and first account name without a YAML parser.

    Returns ``(hostname, username)``, either of which may be None.

    For display only, and only as a fallback when PyYAML is absent. That case is
    the normal one for the single-file build, which people run under whatever
    system Python they have: without this, the two facts most likely to let
    someone recognise their own card -- the hostname and login they typed into
    Raspberry Pi Imager -- would be missing exactly where they matter most.

    Deliberately not a YAML parser. It reads the flat ``hostname:`` key and the
    first ``- name:`` under ``users:``, which is the shape Imager writes, and
    returns None for anything else rather than guessing. Nothing here may inform
    a decision to write; the write path still requires a real parse.
    """
    hostname = None
    match = re.search(r"^hostname:[ \t]*(.+?)[ \t]*$", text, re.MULTILINE)
    if match:
        hostname = match.group(1).strip("\"'") or None

    return hostname, _first_user_name_under_users(text)


def _first_user_name_under_users(text: str) -> str | None:
    """Return the first ``- name:`` value in the top-level ``users:`` block.

    Scoped to that block on purpose: other cloud-config keys hold lists whose
    entries can also carry a ``name``, and a document-wide search would happily
    report one of those as the login account.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.fullmatch(r"users:[ \t]*", line):
            continue
        for entry in lines[index + 1 :]:
            # The block ends at the next top-level key, which is any
            # unindented line that is not itself a list item.
            if entry and not entry[0].isspace() and not entry.lstrip().startswith("-"):
                return None
            found = re.match(r"[ \t]*-[ \t]+name:[ \t]*(.+?)[ \t]*$", entry)
            if found:
                return found.group(1).strip("\"'") or None
    return None


def try_parse_cloud_config(text: str) -> dict | None:
    """Parse a cloud-config document for display, or return None if unreadable.

    The raising :func:`parse_cloud_config` is right when the parse gates a write.
    It is wrong for callers that only want to show the configured hostname on
    screen: a missing PyYAML, or a document cloud-init itself would reject, must
    cost the user a line of output, not the whole run.

    Returns None rather than an empty mapping so "this card configures nothing"
    stays distinguishable from "this could not be read" -- the caller needs that
    difference to avoid reporting an absence it did not actually establish.

    Living here rather than in the CLI keeps the imprecise ``yaml.YAMLError``
    catch next to the only import of yaml, where the type can be named.
    """
    # Function-local for the same reason as parse_cloud_config: PyYAML is
    # optional, and its absence must degrade this to None rather than stop the
    # tool from importing.
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return None

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None

    # An empty or comment-only document is readable and configures nothing,
    # which is the empty mapping -- matching parse_cloud_config, and distinct
    # from the None returned when the document could not be read at all.
    if parsed is None:
        return {}

    # A document whose top level is a list or scalar is valid YAML but not valid
    # cloud-config, and has no keys to report.
    return parsed if isinstance(parsed, dict) else None


def append_runcmd(user_data: str, command: str) -> str:
    """Return ``user_data`` with ``command`` appended to the ``runcmd`` list.

    Edits the document as text so that the comments Raspberry Pi ships in
    ``user-data`` -- and any the user added -- survive. A parse-and-redump
    round trip would discard all of them.

    Appends rather than prepends because the gadget must be configured after
    any host setup the user already scheduled. Creates the key, and the
    ``#cloud-config`` header, when either is missing.

    Idempotent: an identical item already in the document is not added again.

    Raises:
        ValueError: If ``runcmd`` is written in flow style (``runcmd: [a, b]``).
            A block item cannot be appended to a flow sequence, and emitting one
            anyway would make cloud-init discard the entire configuration --
            taking the user account and SSH setup down with it. Refusing lets
            the caller tell the user to merge by hand.

    """
    lines = user_data.splitlines()
    key_index = _find_top_level_runcmd(lines)

    if any(line.strip() == f"- {command}" for line in lines):
        return _ensure_trailing_newline(user_data)

    if key_index is None:
        return _append_new_runcmd_block(user_data, command)

    block_end, indent = _describe_sequence_block(lines, key_index)
    lines.insert(block_end, f"{indent}- {command}")
    return "\n".join(lines) + "\n"


def gadget_mode_runcmds(mode: str) -> tuple[str, ...]:
    """Return the commands that leave the gadget in ``mode``.

    ``rpi-usb-gadget on`` settles on nothing by itself. It creates both
    NetworkManager profiles, activates Shared, leaves Client at ``autoconnect
    no``, and enables the watcher that moves between them -- so every mode here
    has to say what it wants out loud.

    Client and Shared each mean three things: stop the watcher, set both
    profiles' autoconnect, and activate the wanted one now so the first boot does
    not need a reboot to reach it. Both profiles are named because setting only
    the wanted one leaves the other autoconnecting as the vendor left it, and
    which of the two NetworkManager picks for ``usb0`` on the next boot is then a
    race.

    Auto is the watcher left running, and therefore pins nothing: autoconnect
    values set underneath it would either be ignored or fight it.

    Each command tolerates its own failure: on a device without the vendor
    script neither the unit nor the profiles exist, and a non-zero exit from
    ``runcmd`` would abandon the rest of the first-boot configuration.

    Raises:
        ValueError: If ``mode`` is not one of ``MODES``. Anything else would
            silently fall through to one of them, giving the card a different
            configuration from the one asked for.

    """
    if mode not in MODES:
        message = f"unknown gadget mode: {mode!r}"
        raise ValueError(message)

    if mode == AUTO_MODE:
        return (f"systemctl enable --now {ICS_UNIT} || true",)

    wanted = _PROFILES[mode]
    unwanted = _PROFILES[SHARED_MODE if mode == CLIENT_MODE else CLIENT_MODE]
    return (
        f"systemctl disable --now {ICS_UNIT} || true",
        f'nmcli connection modify "{wanted}" connection.autoconnect yes || true',
        f'nmcli connection modify "{unwanted}" connection.autoconnect no || true',
        f'nmcli connection down "{unwanted}" || true',
        f'nmcli connection up "{wanted}" || true',
    )


def pin_gadget_mode(user_data: str, mode: str) -> str:
    """Return ``user_data`` carrying exactly one mode's worth of commands.

    Every other mode's commands are removed first, so re-running the tool with a
    different choice replaces it rather than leaving two in the document. Left
    together they would both run, and the mode would rest on which ``systemctl``
    or ``nmcli`` call happened to be last.

    Assumes the enabling command is already in ``runcmd``: the removal can then
    never empty the sequence, which would leave a ``runcmd:`` key with no items
    for cloud-init to reject.

    What modes share -- Client and Shared both stop the watcher -- is left where
    it is. Removing and re-appending it would reorder ``runcmd`` on every run,
    and a document that changes each time is no longer a card that can be
    re-checked without being rewritten.

    Raises:
        ValueError: If ``mode`` is unknown, or ``runcmd`` uses flow style.

    """
    wanted = gadget_mode_runcmds(mode)
    superseded = {
        command for other in MODES if other != mode for command in gadget_mode_runcmds(other)
    } - set(wanted)

    text = _drop_runcmd_items(user_data, superseded)
    for command in wanted:
        text = append_runcmd(text, command)
    return text


def _drop_runcmd_items(user_data: str, commands: Collection[str]) -> str:
    """Return ``user_data`` without the given items in its ``runcmd`` block.

    Scoped to the block so that a command appearing in a ``write_files`` body --
    a script the card installs, say -- is left alone.

    Raises:
        ValueError: If ``runcmd`` uses flow style.

    """
    lines = user_data.splitlines()
    key_index = _find_top_level_runcmd(lines)
    if key_index is None:
        return _ensure_trailing_newline(user_data)

    block_end, _ = _describe_sequence_block(lines, key_index)
    unwanted = {f"- {command}" for command in commands}
    kept = [
        line
        for index, line in enumerate(lines)
        if not (key_index < index < block_end and line.strip() in unwanted)
    ]
    return "\n".join(kept) + "\n"


def _find_top_level_runcmd(lines: list[str]) -> int | None:
    """Return the index of the column-zero ``runcmd:`` line, or None.

    Raises:
        ValueError: If the key is present but uses flow style.

    """
    return _find_top_level_sequence(lines, "runcmd")


def _find_top_level_sequence(lines: list[str], key: str) -> int | None:
    """Return the index of the column-zero ``<key>:`` line, or None.

    Raises:
        ValueError: If the key is present but uses flow style. A block item
            cannot be appended to a flow sequence, and emitting one anyway would
            make cloud-init discard the whole document.

    """
    for index, line in enumerate(lines):
        match = _TOP_LEVEL_KEY.match(line)
        if not match or match.group(1) != key:
            continue
        remainder = match.group(2).strip()
        if remainder and not remainder.startswith("#"):
            message = f"{key} is written in flow style; merge the entry by hand"
            raise ValueError(message)
        return index
    return None


def append_write_file(user_data: str, path: str, content: str, permissions: str) -> str:
    """Return ``user_data`` with a ``write_files`` entry creating ``path``.

    Edits as text for the same reason as :func:`append_runcmd`: a parse-and-dump
    round trip would strip every comment from the user's document.

    ``content`` is emitted as a literal block scalar, which preserves it
    byte-for-byte including blank lines and leading ``#``. Blank lines are
    written bare rather than padded, since trailing whitespace in a block scalar
    is preserved and would end up in the installed file.

    Idempotent by ``path``: an entry already writing that file is left alone
    rather than duplicated, matching the tool's contract that re-running against
    a prepared card is a no-op.

    Raises:
        ValueError: If ``write_files`` is written in flow style.

    """
    lines = user_data.splitlines()
    key_index = _find_top_level_sequence(lines, "write_files")

    if any(line.strip() == f"- path: {path}" for line in lines):
        return _ensure_trailing_newline(user_data)

    if key_index is None:
        item_indent = _DEFAULT_ITEM_INDENT
        entry = _render_write_file_entry(path, content, permissions, item_indent)
        return _append_new_block(user_data, f"write_files:\n{entry}")

    block_end, item_indent = _describe_sequence_block(lines, key_index)
    entry = _render_write_file_entry(path, content, permissions, item_indent)
    lines.insert(block_end, entry.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _render_write_file_entry(path: str, content: str, permissions: str, item_indent: str) -> str:
    """Return one ``write_files`` list item, indented to ``item_indent``."""
    key_indent = f"{item_indent}  "
    body_indent = f"{key_indent}  "
    body = "\n".join(f"{body_indent}{line}" if line else "" for line in content.splitlines())
    return (
        f"{item_indent}- path: {path}\n"
        f"{key_indent}permissions: '{permissions}'\n"
        f"{key_indent}owner: root:root\n"
        f"{key_indent}content: |\n"
        f"{body}\n"
    )


def _describe_sequence_block(lines: list[str], key_index: int) -> tuple[int, str]:
    """Return the insertion point and item indentation for a sequence block.

    The block runs from just after ``key_index`` to the last line that is blank,
    indented, or a column-zero ``- `` item. The insertion point is immediately
    after the final non-blank line of that block, so a new item is not stranded
    beyond trailing blank lines.
    """
    insert_at = key_index + 1
    cursor = key_index + 1
    indent: str | None = None

    while cursor < len(lines):
        line = lines[cursor]
        if line.strip():
            is_item = _SEQUENCE_ITEM.match(line)
            if not line[0].isspace() and not is_item:
                break
            if indent is None and is_item:
                indent = is_item.group(1)
            insert_at = cursor + 1
        cursor += 1

    return insert_at, _DEFAULT_ITEM_INDENT if indent is None else indent


def _append_new_runcmd_block(user_data: str, command: str) -> str:
    """Return ``user_data`` with a freshly created ``runcmd`` block appended."""
    return _append_new_block(user_data, f"runcmd:\n{_DEFAULT_ITEM_INDENT}- {command}\n")


def _append_new_block(user_data: str, block: str) -> str:
    """Return ``user_data`` with ``block`` appended under a cloud-config header."""
    if not user_data.strip():
        return f"{CLOUD_CONFIG_HEADER}\n\n{block}"

    text = _ensure_trailing_newline(user_data)
    first_content = next(line.strip() for line in text.splitlines() if line.strip())
    if first_content != CLOUD_CONFIG_HEADER:
        # cloud-init ignores a document whose first line is not this header, so
        # our block would be silently dropped without it.
        text = f"{CLOUD_CONFIG_HEADER}\n{text}"

    return f"{text}{block}"


# ---------------------------------------------------------------------------
# Boot partition identification
# ---------------------------------------------------------------------------


def looks_like_boot_partition(path: Path) -> bool:
    """Return whether ``path`` is a Raspberry Pi firmware/boot partition.

    This is the only guard between a mistyped path and the tool writing into an
    unrelated volume, so it requires both text files *and* a firmware artefact.
    Any single marker on its own would also match, for example, an unpacked
    firmware archive or a folder holding a backed-up config.txt.

    Returns False rather than raising for a missing path, so callers can probe
    candidate mount points directly.
    """
    root = Path(path)
    if not root.is_dir():
        return False
    if not all((root / name).is_file() for name in _BOOT_REQUIRED_FILES):
        return False
    return any((root / name).exists() for name in _BOOT_FIRMWARE_MARKERS)

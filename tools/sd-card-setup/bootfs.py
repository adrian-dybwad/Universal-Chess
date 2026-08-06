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
from collections.abc import Sequence
from pathlib import Path

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
        raise ValueError("cmdline.txt is empty; refusing to construct one from scratch")
    if len(content_lines) > 1:
        raise ValueError(
            "cmdline.txt must be a single line; found "
            f"{len(content_lines)}. Refusing to edit a file that is already malformed."
        )
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

    An existing ``modules-load=`` token is extended in place rather than having
    a second one appended: the kernel honours only the last occurrence of a
    repeated parameter, so appending would silently discard whatever the first
    one named. Existing modules keep their position because load order matters
    for dependent modules.

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
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover -- environment-dependent
        raise RuntimeError(
            "PyYAML is required to validate user-data. Install it with: "
            "python3 -m pip install pyyaml"
        ) from exc

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
    try:
        import yaml
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
            raise ValueError(f"{key} is written in flow style; merge the entry by hand")
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

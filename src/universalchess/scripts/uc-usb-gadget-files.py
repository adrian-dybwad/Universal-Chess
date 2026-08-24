#!/usr/bin/env python3
"""Root-owned file surgery for USB Ethernet gadget mode.

Called only by ``uc-usb-gadget-admin``, which is the pinned target of the
package's passwordless-sudo grant. It is a separate script rather than python
embedded in that shell script for two reasons: the code that edits
``cmdline.txt`` decides whether a board still boots, so it must be linted and
directly testable; and the shell helper stays a list of privileged commands,
which is what makes its ``case`` readable as a security boundary.

It carries no privilege of its own. Running as root comes from the caller, so it
sits beside that caller in a root-owned directory and is invoked by absolute
path -- anything able to modify it could already modify the granted helper.

Two jobs, both of which must survive the board losing power mid-write:

``arm-cmdline`` / ``disarm-cmdline``
    Add or remove ``dwc2`` and ``g_ether`` in the kernel command line's single
    ``modules-load=`` parameter. Arming binds the gadget before userspace
    starts, so a host already plugged in at boot enumerates on its first try.

``detach-netplan-eth0`` / ``restore-netplan-eth0``
    The stock image's ``netplan-eth0`` profile matches every ethernet interface,
    so on a board whose only one is ``usb0`` it claims the gadget as a DHCP
    client and fights Shared mode. Detaching moves it aside (keeping a backup so
    Off can put the board back); restoring returns the shipped configuration.

Exit codes: 0 done or nothing to do, 2 usage, 3 refused (the file on disk is not
one this tool will edit), 4 the operation failed and was rolled back.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

OK = 0
USAGE = 2
REFUSED = 3
FAILED = 4

# Verb plus one target path; the netplan verb adds an eth0 flag.
MIN_ARGUMENTS = 2

ARM = "arm-cmdline"
DISARM = "disarm-cmdline"
DETACH = "detach-netplan-eth0"
RESTORE = "restore-netplan-eth0"
ARM_MODULES = "arm-modules-load"
DISARM_MODULES = "disarm-modules-load"
ENSURE_USB0_DHCP = "ensure-usb0-dhcp-netplan"
REMOVE_USB0_DHCP = "remove-usb0-dhcp-netplan"
G_ETHER_LINE = "g_ether\n"
USB0_DHCP_NETPLAN = """\
# Universal Chess: USB Ethernet gadget (client DHCP).
# Armbian's stock netplan only matches e*, so usb0 would otherwise stay unconfigured.
network:
  version: 2
  renderer: networkd
  ethernets:
    usb0:
      dhcp4: true
      dhcp6: true
      optional: true
"""

# Modules the gadget needs bound before userspace, in load order: the controller
# in peripheral mode, then the ethernet function on top of it.
GADGET_MODULES = ("dwc2", "g_ether")
MODULES_PREFIX = "modules-load="
# Conventional anchor: rootwait is where the card-preparation tool
# (tools/sd-card-setup) puts the parameter, so a board prepared either way reads
# the same. Absence is normal, not an error -- the parameter is then appended.
ANCHOR = "rootwait"

NETPLAN_GLOB = "90-NM-*.yaml"
NETPLAN_ETH0_ID = "netplan-eth0"
# netplan reads *.yaml only, so a backup beside the original is inert
# configuration-wise while staying obvious to anyone reading the directory.
BACKUP_SUFFIX = ".uc-backup"
GENERIC_MATCH = "match: {}"
ETH0_MATCH = "match:\n        name: eth0"

DEFAULT_FILE_MODE = 0o644


class RefusedError(Exception):
    """The file on disk is not one this tool is willing to edit."""


def report(message: str) -> None:
    """Write one line to stderr, where the calling shell helper logs it.

    Not a logger: this runs as a short-lived root process invoked by a shell
    script, and its stderr is what that script forwards to the journal.
    """
    sys.stderr.write(message + "\n")


def fsync_directory(directory: Path) -> None:
    """Flush a directory entry so a completed rename is on disk, if possible.

    Best effort by nature: /boot is vfat, which has no journal, so ordering
    cannot be guaranteed there whatever this does. It costs nothing on the
    filesystems that do honour it, and the rename has already happened either
    way, so a failure here is not a failure of the write -- it is reported and
    not raised.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError as error:
        report(f"note: could not open {directory} to flush it: {error}")
        return
    try:
        os.fsync(fd)
    except OSError as error:
        report(f"note: could not flush {directory}: {error}")
    finally:
        os.close(fd)


def write_atomically(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text`` without ever truncating it.

    The new text is written to a temp file in the same directory, flushed to
    disk, then renamed over the target: the old contents stay readable until the
    new ones are complete. Writing the live path directly would leave a window
    in which a power cut -- which is how this board is usually turned off --
    leaves a truncated file, and a truncated ``cmdline.txt`` has no ``root=``
    and does not boot.

    Raises:
        OSError: If the temp file cannot be created, written, or renamed. The
            temp file is removed first, so a failure leaves nothing behind.

    """
    directory = path.parent
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = DEFAULT_FILE_MODE
    fd, temp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}-uc-")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        temp_path.replace(path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    fsync_directory(directory)


def parse_command_line(text: str) -> list[str]:
    """Return the command line's parameters, or refuse text that cannot be one.

    A real ``cmdline.txt`` is one line naming a root device. Anything else is
    what a previously truncated write leaves behind, and editing it would write
    a second broken generation over the evidence.

    Raises:
        RefusedError: If the text is blank, holds more than one line, or has no
            ``root=`` parameter.

    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        message = f"expected one non-empty line, found {len(lines)}"
        raise RefusedError(message)
    tokens = lines[0].split()
    if not any(token.startswith("root=") for token in tokens):
        message = "no root= parameter"
        raise RefusedError(message)
    return tokens


def modules_index(tokens: list[str]) -> int | None:
    """Index of the ``modules-load=`` parameter in ``tokens``, if it has one."""
    return next(
        (i for i, token in enumerate(tokens) if token.startswith(MODULES_PREFIX)),
        None,
    )


def modules_values(token: str) -> list[str]:
    """Return the module names listed by one ``modules-load=`` parameter."""
    return [value for value in token[len(MODULES_PREFIX) :].split(",") if value]


def armed_tokens(tokens: list[str]) -> list[str]:
    """Return ``tokens`` with the gadget modules present exactly once.

    An existing ``modules-load=`` is extended in place, keeping the order of
    what it already lists, because load order matters for dependent modules and
    because a repeated kernel parameter leaves which occurrence wins up to
    whoever reads it.
    """
    updated = list(tokens)
    index = modules_index(updated)
    if index is None:
        parameter = MODULES_PREFIX + ",".join(GADGET_MODULES)
        if ANCHOR in updated:
            updated.insert(updated.index(ANCHOR) + 1, parameter)
        else:
            updated.append(parameter)
        return updated
    values = modules_values(updated[index])
    values.extend(module for module in GADGET_MODULES if module not in values)
    updated[index] = MODULES_PREFIX + ",".join(values)
    return updated


def disarmed_tokens(tokens: list[str]) -> list[str]:
    """Return ``tokens`` with the gadget modules removed from ``modules-load=``.

    Only those two names go: the parameter may list modules this tool never
    added. An emptied parameter is dropped rather than left valueless.
    """
    updated = list(tokens)
    index = modules_index(updated)
    if index is None:
        return updated
    values = [
        value for value in modules_values(updated[index])
        if value not in GADGET_MODULES
    ]
    if values:
        updated[index] = MODULES_PREFIX + ",".join(values)
    else:
        del updated[index]
    return updated


def unusable_reason(text: str, *, arming: bool) -> str | None:
    """Return why a written command line is unusable, or None when it is sound.

    Run against what is actually on disk after the write, so a short write or a
    filesystem that lied about success is caught while the original is still
    recoverable from memory -- before a reboot that might not come back.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return f"not a single line ({len(lines)} lines)"
    tokens = lines[0].split()
    parameters = [token for token in tokens if token.startswith(MODULES_PREFIX)]
    listed = modules_values(parameters[0]) if parameters else []
    if arming:
        wrong_modules = [m for m in GADGET_MODULES if m not in listed]
        module_fault = f"modules-load= does not name {', '.join(wrong_modules)}"
    else:
        wrong_modules = [m for m in GADGET_MODULES if m in listed]
        module_fault = f"modules-load= still names {', '.join(wrong_modules)}"
    faults = [
        (not any(token.startswith("root=") for token in tokens), "no root= parameter"),
        (len(parameters) > 1, f"{len(parameters)} modules-load= parameters"),
        (bool(wrong_modules), module_fault),
    ]
    return next((reason for failed, reason in faults if failed), None)


def edit_command_line(path: Path, *, arming: bool) -> int:  # noqa: PLR0911 - see docstring
    """Arm or disarm the gadget modules in one ``cmdline.txt``.

    A missing file is not an error: the caller offers both the current
    ``/boot/firmware`` and the legacy ``/boot`` location, and a board with
    neither has nothing to arm. Creating one would invent a command line with no
    root device.

    There is one return per outcome the caller distinguishes -- nothing to do,
    refused, unreadable, already correct, written, verified wrong -- because the
    exit code is how the shell helper knows which happened, and folding them
    together would leave it reporting a boot edit it cannot tell apart from a
    file that was never there.
    """
    if not path.exists():
        return OK
    try:
        original = path.read_text(encoding="utf-8")
        tokens = parse_command_line(original)
    except RefusedError as refusal:
        report(f"refusing to edit {path}: {refusal}")
        return REFUSED
    except OSError as error:
        report(f"cannot read {path}: {error}")
        return FAILED

    updated = " ".join(armed_tokens(tokens) if arming else disarmed_tokens(tokens))
    updated += "\n"
    if updated == original:
        return OK

    try:
        write_atomically(path, updated)
    except OSError as error:
        report(f"cannot write {path}: {error}")
        return FAILED

    reason = unusable_reason(path.read_text(encoding="utf-8"), arming=arming)
    if reason is None:
        return OK
    report(f"{path} verified wrong after writing ({reason}); restoring")
    try:
        write_atomically(path, original)
    except OSError as error:
        # Nothing further can be done from here, and saying so precisely matters:
        # this is the one path that can leave the boot configuration changed.
        report(f"CRITICAL: could not restore {path}: {error}")
    return FAILED


def netplan_eth0_files(directory: Path) -> list[Path]:
    """Return the netplan files that configure the generic eth0 profile.

    Matched by content, not by name: the generated file's number varies, and the
    directory also holds the Wi-Fi profile this board's only other network
    depends on, which must never be touched. A file that cannot be read is left
    out and said so -- guessing from its name is how the wrong one gets edited.
    """
    found = []
    for path in sorted(directory.glob(NETPLAN_GLOB)):
        text = read_or_report(path)
        if text is not None and NETPLAN_ETH0_ID in text:
            found.append(path)
    return found


def read_or_report(path: Path) -> str | None:
    """Return ``path``'s text, or None after saying why it could not be read."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        report(f"note: skipping {path}: {error}")
        return None


def back_up_once(path: Path) -> None:
    """Copy ``path`` beside itself, unless a backup is already there.

    Every Client/Shared/Auto apply detaches, so overwriting an existing backup
    would replace the board's original configuration with an already-modified
    copy and leave Off nothing worth restoring.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        return
    write_atomically(backup, path.read_text(encoding="utf-8"))


def detach_netplan_eth0(directory: Path, *, eth0_present: bool) -> int:
    """Stop the generic eth0 profile from claiming usb0.

    With a real eth0 the profile is kept and its match restricted to that
    interface. Without one the profile has no interface left to serve and is
    moved aside entirely, which is also what makes ``usb0`` unmanaged until the
    package's NetworkManager drop-in claims it.
    """
    if not directory.is_dir():
        return OK
    for path in netplan_eth0_files(directory):
        if detach_one(path, eth0_present=eth0_present) != OK:
            return FAILED
    return OK


def detach_one(path: Path, *, eth0_present: bool) -> int:
    """Back up one netplan file, then restrict or remove it."""
    try:
        back_up_once(path)
        if not eth0_present:
            path.unlink()
            return OK
        text = path.read_text(encoding="utf-8")
        if GENERIC_MATCH in text:
            write_atomically(path, text.replace(GENERIC_MATCH, ETH0_MATCH))
    except OSError as error:
        report(f"cannot detach {path}: {error}")
        return FAILED
    return OK


def restore_netplan_eth0(directory: Path) -> int:
    """Put back whatever ``detach_netplan_eth0`` moved aside.

    A board this app never configured has no backup and gets no invented
    profile.
    """
    if not directory.is_dir():
        return OK
    for backup in sorted(directory.glob(f"*{BACKUP_SUFFIX}")):
        if restore_one(backup) != OK:
            return FAILED
    return OK


def restore_one(backup: Path) -> int:
    """Move one backup back over the file it was taken from."""
    target = backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])
    try:
        write_atomically(target, backup.read_text(encoding="utf-8"))
        backup.unlink()
    except OSError as error:
        report(f"cannot restore {target}: {error}")
        return FAILED
    return OK



def arm_modules_load(path: Path) -> int:
    """Write a modules-load.d file that loads g_ether only (no dwc2).

    Orange Pi Zero 2W's musb UDC is already peripheral; dwc2 is the Pi
    overlay and is not present on this SoC. Persist g_ether so client
    mode survives reboot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomically(path, G_ETHER_LINE)
    return OK


def disarm_modules_load(path: Path) -> int:
    """Remove the g_ether modules-load.d file if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        return OK
    return OK


def ensure_usb0_dhcp_netplan(path: Path) -> int:
    """Write a netplan stanza so usb0 gets DHCP under systemd-networkd.

    Armbian's stock ``e*`` match never claims usb0, so without this file
    Client mode would enumerate on the host and stay unaddressed on the board.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomically(path, USB0_DHCP_NETPLAN)
    return OK


def remove_usb0_dhcp_netplan(path: Path) -> int:
    """Remove the usb0 DHCP netplan file if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        return OK
    return OK


def usage() -> int:
    """Report the accepted verbs and return the usage exit code."""
    report(
        "usage: uc-usb-gadget-files.py {arm-cmdline|disarm-cmdline} <cmdline.txt>\n"
        "       uc-usb-gadget-files.py detach-netplan-eth0 <netplan-dir>"
        " {--eth0-present|--eth0-absent}\n"
        "       uc-usb-gadget-files.py restore-netplan-eth0 <netplan-dir>\n"
        "       uc-usb-gadget-files.py {arm-modules-load|disarm-modules-load} "
        "<modules-load.conf>\n"
        "       uc-usb-gadget-files.py {ensure-usb0-dhcp-netplan|remove-usb0-dhcp-netplan} "
        "<netplan.yaml>"
    )
    return USAGE


def main(argv: list[str]) -> int:  # noqa: PLR0911 - one return per verb, plus usage
    """Dispatch one verb. Every unrecognised shape is a usage error, not a guess."""
    if len(argv) < MIN_ARGUMENTS:
        return usage()
    verb, target, *rest = argv
    if verb in (ARM, DISARM) and not rest:
        return edit_command_line(Path(target), arming=verb == ARM)
    if verb == DETACH and rest in (["--eth0-present"], ["--eth0-absent"]):
        return detach_netplan_eth0(
            Path(target), eth0_present=rest == ["--eth0-present"]
        )
    if verb == RESTORE and not rest:
        return restore_netplan_eth0(Path(target))
    if verb == ARM_MODULES and not rest:
        return arm_modules_load(Path(target))
    if verb == DISARM_MODULES and not rest:
        return disarm_modules_load(Path(target))
    if verb == ENSURE_USB0_DHCP and not rest:
        return ensure_usb0_dhcp_netplan(Path(target))
    if verb == REMOVE_USB0_DHCP and not rest:
        return remove_usb0_dhcp_netplan(Path(target))
    return usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Prepare a freshly imaged SD card so the Pi is reachable over a USB cable.

Run this on the *host* machine after Raspberry Pi Imager has written the card
and before the card's first boot. It edits only the FAT boot partition, which is
the only partition readable from Windows and macOS; the root filesystem is ext4
and out of reach.

Why this exists
---------------
Universal Chess is installed over the network, so a board that cannot join Wi-Fi
cannot be set up at all. A USB Ethernet gadget gives an always-available way in.
Enabling it has to happen before first boot, because there is otherwise no way
to reach the device to enable it.

What it changes on the card
---------------------------
``config.txt``   dwc2 overlay in peripheral mode, under an explicit ``[all]``.
``cmdline.txt``  ``modules-load=dwc2,g_ether`` so the gadget is live on the
                 first boot, with no reboot needed.
``user-data``    a cloud-init ``runcmd`` invoking Raspberry Pi's own
                 ``rpi-usb-gadget`` to create the NetworkManager profiles that
                 give ``usb0`` an address and serve DHCP to the host.
``ssh``          an empty file, which Raspberry Pi OS consumes on first boot to
                 enable sshd.

Safety
------
The target is validated as a real boot partition before anything is written,
every modified file is backed up once to ``<name>.uc-orig``, the full diff is
shown and confirmed before any write, and writes are fsynced so the card can be
ejected immediately. Re-running on an already prepared card makes no changes.

Usage:
    python3 enable_usb_gadget.py                 # auto-detect the card
    python3 enable_usb_gadget.py --boot /Volumes/bootfs
    python3 enable_usb_gadget.py --dry-run
"""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import string
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Python puts this script's own directory at the front of sys.path, so the
# sibling module imports without any path manipulation.
import bootfs
import hostcheck
from console import confirm, confirm_default_yes, emit
from hostdns import Diagnosis

BACKUP_SUFFIX = ".uc-orig"

# Files on the boot partition, named once so a typo cannot silently target a
# file that does not exist and be read as "this card does not have one".
CONFIG_NAME = "config.txt"
CMDLINE_NAME = "cmdline.txt"
USER_DATA_NAME = "user-data"

# Raspberry Pi OS writes its build identity here. Absent on a card imaged from
# something else, which is itself worth knowing before writing to it.
ISSUE_NAME = "issue.txt"

BYTES_PER_MB = 1024 * 1024
MB_PER_GB = 1024

# NetworkManager "shared" address that rpi-usb-gadget assigns to usb0.
# The address the Pi serves in rpi-usb-gadget's "shared" mode. It is NOT the
# address reached after `rpi-usb-gadget on`, which selects the "client" profile
# and takes an address from the host's DHCP server instead.
GADGET_ADDRESS = "10.12.194.1"
SHARED_MODE = "shared"

# How the standalone check is invoked, quoted back to the user whenever this run
# cannot complete it.
FIX_DNS_COMMAND = "python3 tools/sd-card-setup/fix_host_dns.py"

# The login-time DNS diagnostic. Numbered below Raspberry Pi's own
# 99-rpi-usb-gadget hint so an actual fault is read before general advice.
MOTD_CHECK_SOURCE = Path(__file__).resolve().parent / "motd-dns-check.sh"
MOTD_CHECK_TARGET = "/etc/update-motd.d/98-universal-chess-dns"
MOTD_CHECK_PERMISSIONS = "0755"


# The single-file build replaces this with the script's own text. None means
# this is the modular source, which reads the file from disk instead -- so
# editing motd-dns-check.sh takes effect immediately, with no rebuild, and the
# checked-in tests always exercise the real script rather than a stale copy.
EMBEDDED_MOTD_CHECK_SCRIPT: str | None = None


def read_motd_check_script() -> str:
    """Return the DNS diagnostic this tool installs on the Pi.

    Raises:
        SystemExit: If the script is neither embedded nor on disk. Writing a
            card without it would produce a board that fails silently in exactly
            the case the script exists to explain, so this fails loudly instead
            of quietly preparing a card that is missing a part.

    """
    if EMBEDDED_MOTD_CHECK_SCRIPT is not None:
        return EMBEDDED_MOTD_CHECK_SCRIPT
    if not MOTD_CHECK_SOURCE.is_file():
        raise SystemExit(f"Missing required script: {MOTD_CHECK_SOURCE}")
    return MOTD_CHECK_SOURCE.read_text(encoding="utf-8")


@dataclass(frozen=True)
class PlannedChange:
    """A single file rewrite, held in memory until the user confirms."""

    path: Path
    original: str
    updated: str

    # What the diff shows in place of ``updated``. The DNS diagnostic is ~55
    # lines of shell that would bury the handful of boot settings actually worth
    # reviewing, and a diff nobody reads is not a safeguard. Only ever an
    # abridgement of ``updated``, never different content; the elision names the
    # source file so it stays checkable.
    display_updated: str | None = None

    @property
    def diff_text(self) -> str:
        """The text to render as the post-change side of the diff."""
        return self.updated if self.display_updated is None else self.display_updated

    @property
    def is_new_file(self) -> bool:
        """Whether this change creates a file that does not exist yet."""
        return not self.path.exists()

    @property
    def has_effect(self) -> bool:
        """Whether applying this change would alter the card."""
        return self.original != self.updated


# ---------------------------------------------------------------------------
# Locating the card
# ---------------------------------------------------------------------------


def candidate_mount_points() -> list[Path]:
    """Return plausible removable-media mount points for the current platform.

    Only directories that already exist are returned; the caller still has to
    confirm each one is a boot partition.
    """
    candidates: list[Path] = []

    if sys.platform == "win32":
        candidates += [Path(f"{letter}:\\") for letter in string.ascii_uppercase[3:]]
    elif sys.platform == "darwin":
        candidates += _children_of(Path("/Volumes"))
    else:
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/mnt")):
            candidates += _children_of(base)
        candidates += _children_of(Path("/media"))

    return [path for path in candidates if path.is_dir()]


def _children_of(base: Path) -> list[Path]:
    """Return the immediate subdirectories of ``base``, or empty if unreadable."""
    try:
        return sorted(child for child in base.iterdir() if child.is_dir())
    except OSError:
        # An unreadable or absent mount root simply yields no candidates; this
        # is a discovery probe, not an operation the user asked for.
        return []


def find_boot_partitions() -> list[Path]:
    """Return every mounted Raspberry Pi boot partition that can be found."""
    return [p for p in candidate_mount_points() if bootfs.looks_like_boot_partition(p)]


@dataclass(frozen=True)
class CardIdentity:
    """Read-only facts about a mounted card, shown so the user can recognise it.

    Every field is optional. A card may be imaged with no cloud-init
    customisation, and a filesystem may withhold any given detail. Each fact is
    therefore omitted when it cannot be read rather than filled in with a
    plausible default: this text exists so someone can tell one card from
    another, and an invented detail defeats the entire purpose.
    """

    path: Path
    image: str | None = None
    written_at: str | None = None
    hostname: str | None = None
    username: str | None = None
    partition_bytes: int | None = None
    free_bytes: int | None = None


def _read_first_line(path: Path) -> str | None:
    """Return the first non-empty line of a small text file, or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _written_at(path: Path) -> str | None:
    """Return the modification date of ``path`` as a local date and time."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")  # noqa: DTZ006


def _cloud_config_identity(text: str) -> tuple[str | None, str | None]:
    """Return the (hostname, first account name) a cloud-config document sets."""
    parsed = bootfs.try_parse_cloud_config(text)
    if parsed is None:
        # No PyYAML, or a document it rejects. The single-file build normally
        # runs under a system Python without PyYAML, so this is the common path
        # for users, not an edge case.
        return bootfs.scan_cloud_config_identity(text)
    hostname = parsed.get("hostname")
    return (hostname if isinstance(hostname, str) else None, _first_username(parsed))


def describe_card(path: Path) -> CardIdentity:
    """Gather identifying facts about a mounted boot partition.

    Reads only; nothing here modifies the card. Called before the user has
    confirmed this is the right card, so it must stay side-effect free.
    """
    hostname, username = None, None
    try:
        user_data = (path / USER_DATA_NAME).read_text(encoding="utf-8")
    except OSError:
        # A card imaged without Imager customisation simply has no user-data.
        user_data = None
    if user_data is not None:
        hostname, username = _cloud_config_identity(user_data)

    try:
        usage = shutil.disk_usage(path)
        partition_bytes, free_bytes = usage.total, usage.free
    except OSError:
        partition_bytes, free_bytes = None, None

    return CardIdentity(
        path=path,
        image=_read_first_line(path / ISSUE_NAME),
        written_at=_written_at(path / CONFIG_NAME),
        hostname=hostname,
        username=username,
        partition_bytes=partition_bytes,
        free_bytes=free_bytes,
    )


def human_size(count: int) -> str:
    """Return a byte count in whole MB, switching to GB above 1024 MB.

    A Pi boot partition is a few hundred MB, so MB is the natural unit here.
    Scaling to GB matters for the opposite case: a path that is not a card at
    all reads as "926.4 GB", which is recognisably an internal disk, where six
    digits of megabytes is just noise.
    """
    megabytes = count / BYTES_PER_MB
    if megabytes >= MB_PER_GB:
        return f"{megabytes / MB_PER_GB:.1f} GB"
    return f"{megabytes:.0f} MB"


def render_card_identity(identity: CardIdentity) -> str:
    """Return the block of text describing a card, for the user to check.

    Pure, so the wording is testable without a card present.
    """
    rows: list[tuple[str, str]] = [("Card", str(identity.path))]
    if identity.image:
        rows.append(("Image", identity.image))
    if identity.written_at:
        rows.append(("Written", identity.written_at))
    if identity.hostname:
        rows.append(("Hostname", f"{identity.hostname}.local"))
    if identity.username:
        rows.append(("Account", identity.username))
    if identity.partition_bytes is not None and identity.free_bytes is not None:
        rows.append(
            (
                "Size",
                f"{human_size(identity.partition_bytes)}, {human_size(identity.free_bytes)} free",
            )
        )

    width = max(len(label) for label, _ in rows)
    lines = [f"  {label.ljust(width)}  {value}" for label, value in rows]

    # The size shown is the small FAT boot partition, not the card. Saying so
    # stops a 512 MB reading from being mistaken for a wrong or faulty card.
    if identity.partition_bytes is not None:
        lines.append("")
        lines.append("  (the size is the boot partition, not the whole card)")
    return "\n".join(lines)


def confirm_card(
    boot: Path,
    *,
    was_detected: bool,
    assume_yes: bool,
    dry_run: bool,
) -> bool:
    """Show what is known about the card, and confirm it if it was auto-detected.

    Returns whether to proceed.

    Only a detected card is queried. Naming one with ``--boot`` is already the
    deliberate choice this prompt asks for, and asking twice teaches people to
    dismiss prompts unread -- which would cost exactly the protection the
    detected-card case depends on. A dry run writes nothing, so it has no
    consent to obtain, and reports the card without stopping.
    """
    emit("Found this card:" if was_detected else "Using this card:")
    emit(render_card_identity(describe_card(boot)))

    if not was_detected or assume_yes or dry_run:
        return True

    emit()
    return confirm("Is this the card you want to prepare?")


def resolve_boot_partition(explicit: str | None) -> Path:
    """Return the boot partition to operate on.

    Raises:
        SystemExit: If the explicit path is not a boot partition, or if
            auto-detection finds zero or more than one candidate. Guessing
            between several cards risks writing to the wrong one.

    """
    if explicit is not None:
        path = Path(explicit)
        if not bootfs.looks_like_boot_partition(path):
            raise SystemExit(
                f"{path} does not look like a Raspberry Pi boot partition "
                "(expected config.txt, cmdline.txt and firmware files)."
            )
        return path

    found = find_boot_partitions()
    if not found:
        raise SystemExit(
            "No Raspberry Pi boot partition found.\n"
            "Insert the freshly imaged card and re-run, or pass --boot with the "
            "path to the mounted boot partition (often /Volumes/bootfs on macOS, "
            "/media/<user>/bootfs on Linux, or a drive letter on Windows)."
        )
    if len(found) > 1:
        listing = "\n".join(f"  {path}" for path in found)
        raise SystemExit(
            f"Multiple boot partitions found:\n{listing}\n"
            "Re-run with --boot to say which card to prepare."
        )
    return found[0]


# ---------------------------------------------------------------------------
# Building the change set
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Return the text of ``path``, or an empty string when it does not exist."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def plan_changes(boot: Path, *, free_uart: bool, enable_ssh: bool) -> list[PlannedChange]:
    """Return the rewrites needed to make the card reachable over USB.

    Args:
        boot: The mounted boot partition.
        free_uart: Also detach the kernel serial console, freeing the UART for
            the DGT Centaur board. Unrelated to USB access, so opt-in.
        enable_ssh: Create the marker file that turns on sshd at first boot.

    """
    changes: list[PlannedChange] = []

    config_path = boot / CONFIG_NAME
    config_original = _read(config_path)
    changes.append(
        PlannedChange(
            path=config_path,
            original=config_original,
            updated=bootfs.enable_dwc2_overlay(config_original),
        )
    )

    cmdline_path = boot / CMDLINE_NAME
    cmdline_original = _read(cmdline_path)
    cmdline_updated = bootfs.add_modules_load(cmdline_original, bootfs.GADGET_MODULES)
    if free_uart:
        cmdline_updated = bootfs.remove_serial_console(cmdline_updated)
    changes.append(
        PlannedChange(
            path=cmdline_path,
            original=cmdline_original,
            updated=cmdline_updated,
        )
    )

    user_data_path = boot / USER_DATA_NAME
    user_data_original = _read(user_data_path)
    motd_script = read_motd_check_script()
    elision = (
        f"# {MOTD_CHECK_SOURCE.name} verbatim, "
        f"{len(motd_script.splitlines())} lines, not shown here\n"
    )

    def with_gadget_setup(script_content: str) -> str:
        return bootfs.append_runcmd(
            bootfs.append_write_file(
                user_data_original,
                MOTD_CHECK_TARGET,
                script_content,
                MOTD_CHECK_PERMISSIONS,
            ),
            bootfs.GADGET_RUNCMD,
        )

    changes.append(
        PlannedChange(
            path=user_data_path,
            original=user_data_original,
            updated=with_gadget_setup(motd_script),
            display_updated=with_gadget_setup(elision),
        )
    )

    if enable_ssh:
        changes.append(PlannedChange(path=boot / "ssh", original="", updated=""))

    return changes


def render_diff(change: PlannedChange) -> str:
    """Return a unified diff for ``change``, or a note for an empty new file."""
    if change.is_new_file and not change.updated:
        return f"create empty file {change.path.name}\n"
    return "".join(
        _terminate_diff_line(line)
        for line in difflib.unified_diff(
            change.original.splitlines(keepends=True),
            change.diff_text.splitlines(keepends=True),
            fromfile=f"{change.path.name} (current)",
            tofile=f"{change.path.name} (new)",
        )
    )


def _terminate_diff_line(line: str) -> str:
    """Return ``line`` newline-terminated, flagging a missing final newline.

    difflib reproduces each line's own ending, so a file that does not end in a
    newline -- which Raspberry Pi Imager's cmdline.txt does not -- yields a diff
    line that runs straight into the next one on screen, making the old and new
    text look like a single corrupted line.
    """
    if line.endswith("\n"):
        return line
    return f"{line}\n\\ No newline at end of file\n"


# ---------------------------------------------------------------------------
# Validation and reporting
# ---------------------------------------------------------------------------


def _serial_interface_warning(parsed: dict) -> str | None:
    """Return a warning when ``rpi.interfaces.serial`` is a bare boolean.

    cloud-init's ``cc_raspberry_pi`` treats a plain ``serial: true`` as "enable
    the console", and on anything below a Pi 5 it then forces the hardware UART
    on as well. It enables the console via ``raspi-config do_serial 0``, which
    re-inserts ``console=serial0,115200`` immediately before ``root=`` whenever
    no console token is present -- precisely the state ``--free-uart`` leaves
    the card in. The Centaur board shares that UART, so the console must be off
    while the hardware UART stays on.
    """
    interfaces = parsed.get("rpi")
    if not isinstance(interfaces, dict):
        return None
    serial = interfaces.get("interfaces", {})
    if not isinstance(serial, dict) or not isinstance(serial.get("serial"), bool):
        return None
    if not serial["serial"]:
        return None
    return (
        "user-data sets 'rpi.interfaces.serial: true', which enables the serial "
        "login console. At first boot raspi-config re-inserts console=serial0 "
        "before root= if no console token is present, so this silently undoes "
        "--free-uart. The DGT Centaur board shares that UART. Change it to:\n"
        "        serial:\n"
        "          console: false\n"
        "          hardware: true"
    )


def validate_user_data(text: str) -> list[str]:
    """Return warnings about a cloud-config document, after checking it parses.

    Raises:
        SystemExit: If the document does not parse. Writing an invalid
            cloud-config makes cloud-init discard the whole file on first boot,
            silently dropping the user account and SSH keys along with the
            gadget setup -- a card that boots but can never be logged into.

    """
    try:
        parsed = bootfs.parse_cloud_config(text)
    except RuntimeError as exc:
        return [
            f"{exc}",
            "Skipping user-data validation. The edit is still applied.",
        ]
    except Exception as exc:
        raise SystemExit(
            f"Refusing to write: the resulting user-data is not valid YAML ({exc}). "
            "Please report this along with your user-data file."
        ) from exc

    warnings: list[str] = []
    if not parsed.get("users"):
        warnings.append(
            "user-data defines no 'users:' -- if Raspberry Pi Imager did not set "
            "up an account, you will have no way to log in over USB."
        )
    rpi_section = parsed.get("rpi")
    if isinstance(rpi_section, dict) and rpi_section.get("enable_usb_gadget"):
        warnings.append(
            "user-data already sets rpi.enable_usb_gadget. That path runs under a "
            "15-second timeout that a Pi Zero 2 W does not meet; the runcmd added "
            "here runs afterwards and is unaffected. Leaving both is harmless."
        )

    serial_warning = _serial_interface_warning(parsed)
    if serial_warning:
        warnings.append(serial_warning)

    return warnings


def _first_username(parsed: dict) -> str | None:
    """Return the first account name in a cloud-config ``users:`` list.

    Returns None when no name can be read, so callers omit the hint rather than
    print an account that does not exist on the card.
    """
    users = parsed.get("users")
    if not isinstance(users, list):
        return None
    for user in users:
        if isinstance(user, dict) and isinstance(user.get("name"), str):
            return user["name"]
    return None


def report_access_details(user_data: str) -> None:
    """Print how to reach the board, and what the host has to provide.

    ``rpi-usb-gadget on`` activates its "client" profile, so the Pi is a DHCP
    *client* and its address is whatever the host hands out -- not a fixed one.
    The fixed 10.12.194.1 belongs to the separate "shared" profile, which is not
    what ``on`` selects.
    """
    hostname, username = _cloud_config_identity(user_data)

    emit()
    emit("Once the card has booted with a USB cable to this machine:")
    if hostname:
        emit(f"  http://{hostname}.local/   (needs mDNS on this machine)")
        # Without a parsed account the ssh line is omitted rather than guessed:
        # printing a wrong username sends the user chasing an auth failure.
        if username:
            emit(f"  ssh {username}@{hostname}.local")
    emit()
    emit("The Pi requests an address by DHCP, so it has no fixed IP. The host")
    emit("must be sharing its connection over the USB interface:")
    emit("  macOS    System Settings > General > Sharing > Internet Sharing")
    emit("  Windows  Network Connections > adapter > Sharing tab (ICS)")
    emit("  Linux    NetworkManager 'Shared to other computers' on the usb0 link")
    emit()
    emit("If mDNS does not resolve, find the address the host leased it:")
    emit("  macOS    arp -an | grep bridge     (or /var/db/dhcpd_leases)")
    emit("  Windows  arp -a")
    emit("  Linux    ip neigh show dev <usb interface>")
    emit()
    emit(f"To avoid host-side sharing entirely, run 'sudo rpi-usb-gadget {SHARED_MODE}'")
    emit(f"on the Pi. It then serves {GADGET_ADDRESS} and its own DHCP, at the cost of")
    emit("giving the Pi no route to the internet.")
    emit()
    emit("Windows hosts need the one-time RNDIS driver from")
    emit("  https://github.com/raspberrypi/rpi-usb-gadget/releases")
    emit("On a Pi Zero, use the micro-USB port next to the mini-HDMI, not PWR IN.")


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_change(change: PlannedChange) -> None:
    """Back up and write a single file, flushing to the card before returning.

    The backup is taken only the first time a file is modified, so re-running
    never overwrites the pristine original with an already-edited copy.

    Content is fsynced rather than left to the page cache because the expected
    next action is physically removing the card.
    """
    if change.original and not change.is_new_file:
        backup = change.path.with_name(change.path.name + BACKUP_SUFFIX)
        if not backup.exists():
            backup.write_text(change.original, encoding="utf-8")

    with change.path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(change.updated)
        handle.flush()
        os.fsync(handle.fileno())


def should_wait_for_board(args: argparse.Namespace) -> bool:
    """Return whether this run should continue into the host DNS check.

    Waiting is for someone sitting at the machine about to plug a board in.
    A run with no terminal attached -- a script, a CI job, a piped invocation --
    has nobody to do that, so it must not stall for minutes on hardware that
    will never arrive. ``--wait`` overrides this for deliberate automation.
    """
    if args.no_wait:
        return False
    if args.wait:
        return True
    return sys.stdin.isatty()


def guided_host_check(
    run: hostcheck.CommandRunner,
    *,
    assume_yes: bool,
    timeout_seconds: int,
) -> int:
    """Wait for the board to come up, then check DNS on the link it creates.

    This is the second half of preparing a board, and it cannot be done earlier:
    the shared interface does not exist until the Pi enumerates over USB, which
    is necessarily after the card has been written and moved. Keeping it in the
    same run is what spares the user a second tool and a second decision.

    Returns an exit code. A board that never appears is not treated as a
    failure of the card preparation, which already succeeded.
    """
    key = hostcheck.detect_platform(sys.platform)
    if key is None:
        emit(f"Skipping the DNS check: unsupported host platform {sys.platform}.")
        return 0
    support = hostcheck.PLATFORMS[key]

    emit()
    emit("Turn on internet connection sharing for the USB interface first: the")
    emit("Pi needs a route out, and on macOS the interface watched for below is")
    emit("the bridge that Internet Sharing creates.")
    emit("Then eject the card, put it in the Pi, and connect the USB cable.")
    if not (assume_yes or confirm_default_yes("Wait for the board and check DNS?")):
        emit(f"Skipped. Run {FIX_DNS_COMMAND} later if names do not resolve.")
        return 0

    emit()
    emit(
        f"Waiting up to {timeout_seconds}s for the {support.label} end of the "
        "link to appear. A Pi Zero's first boot is slow; Ctrl-C to stop."
    )

    try:
        shared = hostcheck.wait_for_shared_link(support, run, timeout_seconds=timeout_seconds)
    except KeyboardInterrupt:
        emit()
        emit(f"Stopped waiting. Run {FIX_DNS_COMMAND} once the board is up.")
        return 0

    if shared is None:
        emit()
        emit("The shared interface never appeared. The card is still prepared;")
        emit("check the host is sharing its connection, then run")
        emit(f"  {FIX_DNS_COMMAND}")
        return 0

    emit(f"Link is up: {shared.name} at {shared.address}")
    emit()

    findings = hostcheck.investigate(support, run, shared.address)
    hostcheck.report(findings)

    if findings.diagnosis is not Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE:
        return 0

    return hostcheck.repair(findings, run, assume_yes=assume_yes)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="enable_usb_gadget",
        description=(
            "Prepare a freshly imaged Raspberry Pi SD card so the board is "
            "reachable over a USB cable before it has any network."
        ),
    )
    parser.add_argument(
        "--boot",
        metavar="PATH",
        help="Path to the mounted boot partition. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change and exit without writing.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply without the interactive confirmation.",
    )
    parser.add_argument(
        "--check-dns",
        action="store_true",
        help=(
            "Skip the card entirely and only check DNS on an already-connected "
            "board. Use when a working board stops resolving names."
        ),
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="With --check-dns, offer to restart a resolver that missed the link.",
    )
    parser.add_argument(
        "--shared-address",
        metavar="ADDR",
        help=(
            "With --check-dns, use this as the shared link address instead of "
            "detecting it. For hosts sharing over an unrecognised interface."
        ),
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help=(
            "Wait for the board even with no terminal attached. Off by default "
            "so scripted runs do not stall."
        ),
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "Stop after writing the card, instead of waiting for the board and "
            "checking DNS on the link."
        ),
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=hostcheck.DEFAULT_WAIT_SECONDS,
        metavar="SECONDS",
        help=(
            f"How long to wait for the board to appear (default {hostcheck.DEFAULT_WAIT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--free-uart",
        action="store_true",
        help=(
            "Also detach the kernel serial console so the UART is free for the "
            "DGT Centaur board. Not needed for USB access."
        ),
    )
    parser.add_argument(
        "--no-ssh",
        action="store_true",
        help="Do not create the boot-partition marker that enables sshd.",
    )
    return parser


def check_dns_only(
    run: hostcheck.CommandRunner,
    *,
    fix: bool,
    assume_yes: bool,
    shared_address: str | None = None,
) -> int:
    """Diagnose DNS on an already-connected board, without touching a card.

    The mode the Pi's own login banner points at. By the time a board stops
    resolving names its card was prepared long ago and is back in the device, so
    this path must not look for one, let alone ask to write to it.
    """
    key = hostcheck.detect_platform(sys.platform)
    if key is None:
        emit(f"Unsupported host platform: {sys.platform}")
        return 2

    findings = hostcheck.investigate(hostcheck.PLATFORMS[key], run, shared_address)
    hostcheck.report(findings)

    if findings.diagnosis is not Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE:
        return 0 if findings.diagnosis is Diagnosis.HEALTHY else 1

    if not (fix or assume_yes):
        emit()
        emit("Re-run with --fix to restart the resolver.")
        return 1

    return hostcheck.repair(findings, run, assume_yes=assume_yes)


def continue_to_board(
    args: argparse.Namespace,
    run: hostcheck.CommandRunner,
) -> int:
    """Hand off from the prepared card to the board it is about to run in.

    Reached from both the just-written and the already-prepared paths, which
    differ only in what happened before them: in either case the card is ready
    and the next step is the same physical move. Keeping it in one place is what
    stops the two paths drifting into giving different closing instructions.
    """
    if not should_wait_for_board(args):
        emit()
        emit("Eject the card, put it in the Pi, and connect the USB cable.")
        emit(f"If names do not resolve, run {FIX_DNS_COMMAND}")
        return 0

    return guided_host_check(run, assume_yes=args.yes, timeout_seconds=args.wait_timeout)


def main(
    argv: list[str] | None = None,
    run: hostcheck.CommandRunner = hostcheck.run_command,
) -> int:
    """Prepare the card. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    if args.check_dns:
        return check_dns_only(
            run,
            fix=args.fix,
            assume_yes=args.yes,
            shared_address=args.shared_address,
        )

    boot = resolve_boot_partition(args.boot)

    if not confirm_card(
        boot,
        was_detected=args.boot is None,
        assume_yes=args.yes,
        dry_run=args.dry_run,
    ):
        emit("Aborted; nothing written.")
        return 1

    try:
        changes = plan_changes(boot, free_uart=args.free_uart, enable_ssh=not args.no_ssh)
    except ValueError as exc:
        # bootfs refuses rather than guessing whenever an existing file is
        # shaped in a way it cannot safely extend. Surfacing that as a plain
        # message beats a traceback for someone preparing a card.
        raise SystemExit(
            f"Cannot prepare this card automatically: {exc}\n"
            "Nothing has been written. tools/sd-card-setup/README.md lists the "
            "manual equivalent of each change."
        ) from exc

    user_data = next(c for c in changes if c.path.name == USER_DATA_NAME)
    for warning in validate_user_data(user_data.updated):
        emit(f"  note: {warning}")

    effective = [c for c in changes if c.has_effect or c.is_new_file]
    if not effective:
        emit()
        emit("This card is already prepared for USB gadget access. Nothing to do.")
        report_access_details(user_data.updated)
        # An already-prepared card is the common case on a second run, and the
        # reason for that run is usually that something is not working.
        return 0 if args.dry_run else continue_to_board(args, run)

    emit()
    for change in effective:
        emit(render_diff(change))

    if args.dry_run:
        emit("Dry run: nothing written.")
        return 0

    if not args.yes and not confirm(f"Apply these changes to {boot}?"):
        emit("Aborted; nothing written.")
        return 1

    for change in effective:
        apply_change(change)

    emit(f"Done. Originals saved alongside as *{BACKUP_SUFFIX}.")
    report_access_details(user_data.updated)

    return continue_to_board(args, run)


if __name__ == "__main__":
    sys.exit(main())

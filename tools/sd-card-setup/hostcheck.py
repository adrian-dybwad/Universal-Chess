"""Inspecting and repairing DNS service on a host's USB gadget link.

The I/O half of the host-side check: runs commands, reduces their output to a
diagnosis via the pure functions in :mod:`hostdns`, and where the remedy has
been verified, offers to restart the resolver responsible.

This is a library, not an entry point. Both ``fix_host_dns.py`` (run on its own
when a board stops resolving) and ``enable_usb_gadget.py`` (which continues into
this check after preparing a card) drive it, so neither depends on the other.

Repair is confined to platforms where the fix has actually been observed to
work. On macOS, restarting the owning resolver was confirmed to rebind the
bridge. Elsewhere this reports and prints the command instead: during the
investigation behind this tool three plausible macOS remedies turned out to do
nothing, and automating an unverified one is worse than printing it.
"""

from __future__ import annotations

import re
import shlex
import subprocess  # nosec B404 - runs a fixed table of inspection commands only
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import hostdns
from console import confirm, emit
from hostdns import Diagnosis, Interface, Listener

CommandRunner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class PlatformSupport:
    """How to inspect a given host, and whether its repair is proven."""

    label: str
    listener_command: tuple[str, ...]
    listener_parser: Callable[[str], tuple[Listener, ...]]
    interface_command: tuple[str, ...]
    interface_parser: Callable[[str], tuple[Interface, ...]]
    # Name prefixes of the interface facing the Pi. macOS bridges Internet
    # Sharing onto bridgeN; Linux presents CDC-ECM as usb0 or enx<mac>.
    shared_prefixes: tuple[str, ...]
    # Reveals which process owns each socket. Needs privilege, so it is only run
    # after the user consents.
    owner_command: tuple[str, ...] | None
    # None means no remedy has been verified on this platform, so the tool
    # reports and stops instead of acting.
    restart_command: Callable[[int], tuple[str, ...]] | None
    manual_hint: str


def _macos_restart(pid: int) -> tuple[str, ...]:
    """Return the command to restart a launchd-supervised resolver.

    Targets the pid rather than the process name because lsof truncates COMMAND
    to nine characters, so a longer daemon name would not match killall.
    """
    return ("sudo", "kill", str(pid))


PLATFORMS: dict[str, PlatformSupport] = {
    "macos": PlatformSupport(
        label="macOS",
        listener_command=("netstat", "-an", "-p", "udp"),
        listener_parser=hostdns.parse_bsd_listeners,
        interface_command=("ifconfig",),
        interface_parser=hostdns.parse_bsd_interfaces,
        shared_prefixes=("bridge",),
        owner_command=("sudo", "lsof", "+c", "0", "-nP", "-iUDP:53"),
        restart_command=_macos_restart,
        manual_hint="sudo lsof +c 0 -nP -iUDP:53",
    ),
    "linux": PlatformSupport(
        label="Linux",
        listener_command=("ss", "-lunp"),
        listener_parser=hostdns.parse_ss_listeners,
        interface_command=("ip", "-4", "addr", "show"),
        interface_parser=hostdns.parse_ip_addr_interfaces,
        shared_prefixes=("usb", "enx"),
        owner_command=("sudo", "ss", "-lunp"),
        restart_command=None,
        manual_hint="sudo systemctl restart <unit owning port 53>",
    ),
    "windows": PlatformSupport(
        label="Windows",
        listener_command=("netstat", "-ano", "-p", "UDP"),
        listener_parser=hostdns.parse_windows_listeners,
        interface_command=("ipconfig",),
        interface_parser=lambda _text: (),
        shared_prefixes=(),
        owner_command=None,
        restart_command=None,
        manual_hint="restart the service owning port 53 via services.msc",
    ),
}


def detect_platform(sys_platform: str) -> str | None:
    """Return the :data:`PLATFORMS` key for a ``sys.platform`` value."""
    if sys_platform == "darwin":
        return "macos"
    if sys_platform.startswith("linux"):
        return "linux"
    if sys_platform in {"win32", "cygwin"}:
        return "windows"
    return None


def run_command(command: Sequence[str]) -> str:
    """Return the stdout of ``command``, or "" if it cannot be run.

    A missing tool is not fatal: the caller reports what it could not determine
    rather than aborting, since a partial diagnosis is still useful.
    """
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 - argv comes from PLATFORMS, never from user input
            list(command), capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout


_LSOF_ROW = re.compile(r"^(\S+)\s+(\d+)\s")


def parse_lsof_owners(text: str) -> dict[str, tuple[int, str]]:
    """Map each bound address to the (pid, command) holding it.

    Only IPv4 rows whose NAME is a plain ``address:port`` are kept; lsof also
    emits connected pairs (``local->remote``) and bracketed IPv6, neither of
    which is a listening socket on this link.
    """
    owners: dict[str, tuple[int, str]] = {}
    for line in text.splitlines():
        match = _LSOF_ROW.match(line)
        if not match:
            continue
        endpoint = line.split()[-1]
        if "->" in endpoint or endpoint.startswith("["):
            continue
        split = hostdns.split_host_port(endpoint, ":")
        if split is None:
            continue
        address, port = split
        if port == str(hostdns.DNS_PORT) and hostdns.is_ipv4(address):
            owners[address] = (int(match.group(2)), match.group(1))
    return owners


def is_supervised(pid: int, run: CommandRunner) -> bool:
    """Return whether ``pid`` is parented by init/launchd and so gets restarted.

    Restarting a resolver nothing supervises would leave the host with no DNS at
    all -- strictly worse than the fault being repaired -- so this gates the
    repair. A daemon adopted by pid 1 is under launchd on macOS or systemd on
    Linux, both of which relaunch it. Verified against the dnsmasq and
    mDNSResponder processes on the host that motivated this tool.
    """
    output = run(("ps", "-o", "ppid=", "-p", str(pid)))
    stripped = output.strip()
    return stripped.isdigit() and int(stripped) == 1


@dataclass(frozen=True)
class Findings:
    """Everything the tool established about the host in one pass."""

    platform: PlatformSupport
    shared: Interface | None
    listeners: tuple[Listener, ...]
    diagnosis: Diagnosis
    unparseable: bool


def investigate(
    support: PlatformSupport, run: CommandRunner, shared_override: str | None
) -> Findings:
    """Gather host state and reduce it to a diagnosis. Runs nothing privileged."""
    listener_output = run(support.listener_command)
    listeners = support.listener_parser(listener_output)

    if shared_override:
        shared: Interface | None = Interface(name="(given)", address=shared_override)
    else:
        interfaces = support.interface_parser(run(support.interface_command))
        shared = hostdns.find_shared_interface(interfaces, support.shared_prefixes)

    return Findings(
        platform=support,
        shared=shared,
        listeners=listeners,
        diagnosis=hostdns.diagnose(shared, listeners),
        unparseable=hostdns.parse_failed(listener_output, listeners),
    )


_EXPLANATIONS: dict[Diagnosis, str] = {
    Diagnosis.HEALTHY: (
        "Something is answering DNS on the shared link. If the Pi still cannot\n"
        "resolve, the fault is not here -- check its own resolv.conf."
    ),
    Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE: (
        "A resolver is running but is not bound to the shared link, so the Pi is\n"
        "being told to use an address where nothing is listening. This happens\n"
        "when the resolver started before the USB bridge existed; it binds once\n"
        "at startup and never re-enumerates."
    ),
    Diagnosis.NO_RESOLVER: (
        "Nothing is listening on port 53 at all, so the host is advertising a DNS\n"
        "server it does not run. This is a different fault from the usual one and\n"
        "has no verified remedy here."
    ),
    Diagnosis.NO_SHARED_INTERFACE: (
        "No shared USB interface was found. Connect the Pi and turn on the host's\n"
        "connection sharing, then run this again. Use --shared-address if the\n"
        "interface exists but was not recognised."
    ),
}


def report(findings: Findings) -> None:
    """Print the diagnosis and the evidence behind it."""
    emit(f"Host: {findings.platform.label}")
    if findings.shared:
        emit(f"Shared link: {findings.shared.name} at {findings.shared.address}")
    else:
        emit("Shared link: not found")

    if findings.listeners:
        emit("Listening on port 53:")
        for listener in findings.listeners:
            emit(
                f"  {listener.address}"
                + (f"  ({listener.process}, pid {listener.pid})" if listener.process else "")
            )
    else:
        emit("Listening on port 53: nothing")

    emit()
    if findings.unparseable:
        emit(
            "Warning: the port-53 output mentioned port 53 but could not be\n"
            "interpreted, so this diagnosis may be wrong. Check by hand with:\n"
            f"  {' '.join(findings.platform.listener_command)}"
        )
        emit()

    emit(f"Diagnosis: {findings.diagnosis.value}")
    emit(_EXPLANATIONS[findings.diagnosis])


def repair(findings: Findings, run: CommandRunner, *, assume_yes: bool) -> int:
    """Restart the resolver that missed the shared link. Returns an exit code."""
    support = findings.platform

    if support.restart_command is None:
        emit()
        emit(
            f"No repair for {support.label} has been verified, so nothing will be\n"
            "run automatically. Identify the owner of port 53 and restart it:"
        )
        emit(f"  {support.manual_hint}")
        return 1

    if support.owner_command is None:
        emit("Cannot identify the owning process on this platform.")
        return 1

    emit()
    emit("Identifying which process holds port 53 requires elevated privileges:")
    emit(f"  {' '.join(support.owner_command)}")
    if not (assume_yes or confirm("Run it?")):
        emit("Stopped. Nothing was changed.")
        return 1

    owners = parse_lsof_owners(run(support.owner_command))
    if not owners:
        emit("Could not determine the owner. Nothing was changed.")
        return 1

    # Any owner will do: they are the same process in every case observed, and
    # restarting it rebinds all of its sockets regardless of which one we name.
    pid, command = next(iter(owners.values()))
    emit()
    emit(f"Port 53 is held by {command} (pid {pid}), bound to: {', '.join(sorted(owners))}")

    if not is_supervised(pid, run):
        emit()
        emit(
            f"{command} is not supervised by the system launcher, so restarting it\n"
            "would leave this host with no DNS at all. Refusing. Restart it the\n"
            "way it was started instead."
        )
        return 1

    restart = support.restart_command(pid)
    emit()
    emit("It is supervised, so it will be relaunched automatically after:")
    emit(f"  {shlex.join(restart)}")
    if not (assume_yes or confirm(f"Restart {command}?")):
        emit("Stopped. Nothing was changed.")
        return 1

    run(restart)
    emit()
    emit(f"Restarted {command}. Re-run this tool to confirm it bound the link.")
    return 0


# A Pi Zero's first boot runs cloud-init on slow hardware, so the bridge can
# take minutes to appear. Long enough not to give up early, short enough that an
# unattended run does not hang indefinitely.
DEFAULT_WAIT_SECONDS = 300
POLL_SECONDS = 3


def wait_for_shared_link(
    support: PlatformSupport,
    run: CommandRunner,
    *,
    timeout_seconds: int = DEFAULT_WAIT_SECONDS,
    poll_seconds: int = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Interface | None:
    """Poll until the host creates the interface facing the Pi.

    Returns the interface, or None if it never appeared within the timeout.

    The bridge does not exist until the board enumerates over USB, so this is
    the join between preparing a card and checking the link it eventually
    creates. Clock and sleep are injected so tests need not actually wait.
    """
    deadline = now() + timeout_seconds
    while True:
        interfaces = support.interface_parser(run(support.interface_command))
        found = hostdns.find_shared_interface(interfaces, support.shared_prefixes)
        if found is not None:
            return found
        if now() >= deadline:
            return None
        sleep(poll_seconds)

"""Pure analysis of a host's DNS listener state, for the USB gadget link.

The fault this models: a resolver enumerates interfaces once when it starts and
binds one socket per address it finds. The USB gadget bridge is created later --
it cannot exist until the Pi enumerates -- so a resolver started at boot never
binds it. DHCP still hands the Pi the bridge address as its nameserver, so the
board is told to ask an address where nothing is listening. Routing is
unaffected, which is what makes the failure so hard to read.

This was first seen with a Homebrew dnsmasq on macOS that had been running for
13 days, still bound to an address from a network the machine had since left.
The daemon is incidental: mDNSResponder, unbound or any other resolver reaches
the same state by the same route, so nothing here is keyed to a process name.

Everything in this module is a pure function over captured command output, so
the parsers can be tested against real fixtures rather than a live host. All I/O
lives in ``fix_host_dns.py``.

Only IPv4 is considered. The gadget link is IPv4, and macOS ``netstat``
truncates long IPv6 addresses mid-string, so its IPv6 rows cannot be parsed
reliably in any case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

DNS_PORT = 53

# Internet Sharing on macOS and ICS on Windows both use fixed defaults. They are
# starting points for the report, not assumptions the logic depends on -- the
# address is detected from the host and can be overridden on the command line.
MACOS_SHARING_ADDRESS = "192.168.2.1"
WINDOWS_ICS_ADDRESS = "192.168.137.1"

# The address a resolver reports when it bound every interface rather than each
# one individually. Such a resolver answers on interfaces created after it
# started and so cannot exhibit the fault this module detects. This is a value
# to recognise in captured output; nothing here opens a socket.
WILDCARD_ADDRESS = "0.0.0.0"  # noqa: S104  # nosec B104 - compared against, never bound

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# Column layouts of the command output each parser reads. Named because getting
# one wrong is silent: the parser finds nothing and the host is reported as
# having no resolver at all, which points the user at the wrong fault entirely.
#
# BSD netstat -an -p udp:  proto  Recv-Q  Send-Q  Local Address  Foreign Address
_BSD_LOCAL_ADDRESS = 3
# Linux ss -lunp:  State  Recv-Q  Send-Q  Local Address:Port  Peer Address:Port
_SS_LOCAL_ADDRESS = 3
# Windows netstat -ano -p UDP:  Proto  Local Address  Foreign Address  PID
_WINDOWS_LOCAL_ADDRESS = 1
_WINDOWS_MIN_FIELDS = 3
# An ifconfig or ip address line needs at least the "inet" keyword and a value.
_ADDRESS_LINE_MIN_FIELDS = 2


@dataclass(frozen=True)
class Listener:
    """A socket bound to :data:`DNS_PORT` on a specific local address."""

    address: str
    pid: int | None = None
    process: str | None = None


@dataclass(frozen=True)
class Interface:
    """An interface carrying a single IPv4 address."""

    name: str
    address: str


class Diagnosis(Enum):
    """The mutually exclusive states this tool distinguishes."""

    HEALTHY = "healthy"
    RESOLVER_MISSED_SHARED_INTERFACE = "resolver_missed_shared_interface"
    NO_RESOLVER = "no_resolver"
    NO_SHARED_INTERFACE = "no_shared_interface"


def is_ipv4(text: str) -> bool:
    """Return whether ``text`` is a dotted-quad address."""
    return bool(_IPV4.match(text))


def split_host_port(endpoint: str, separator: str) -> tuple[str, str] | None:
    """Split ``address<sep>port`` on the final separator.

    The last separator is the delimiter because it is the only one that is
    unambiguous: BSD renders endpoints as ``127.0.0.1.53``, where dots serve as
    both address and port separators.

    Returns None when there is no separator to split on.
    """
    head, found, tail = endpoint.rpartition(separator)
    if not found:
        return None
    return head, tail


# ---------------------------------------------------------------------------
# Listener parsing
# ---------------------------------------------------------------------------


def parse_bsd_listeners(text: str) -> tuple[Listener, ...]:
    """Return IPv4 port-53 listeners from ``netstat -an -p udp`` on macOS/BSD.

    Only ``udp4`` rows are read. macOS truncates long addresses to fit the
    column, which mangles IPv6 -- ``fe80::18f3:f818:.53`` is a real observed row
    -- so those rows carry no usable address.
    """
    listeners = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) <= _BSD_LOCAL_ADDRESS or fields[0] != "udp4":
            continue
        split = split_host_port(fields[_BSD_LOCAL_ADDRESS], ".")
        if split is None:
            continue
        address, port = split
        if port == str(DNS_PORT) and is_ipv4(address):
            listeners.append(Listener(address=address))
    return tuple(listeners)


def parse_ss_listeners(text: str) -> tuple[Listener, ...]:
    """Return IPv4 port-53 listeners from ``ss -lunp`` on Linux.

    ``ss`` writes the wildcard as either ``0.0.0.0`` or ``*`` depending on the
    address family it bound, and both mean "every address", so both are
    normalised to ``0.0.0.0``. The header line is skipped by requiring the
    state column, since ``ss`` runs its header columns together
    (``Peer Address:PortProcess``) and cannot be split on whitespace reliably.
    """
    listeners = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) <= _SS_LOCAL_ADDRESS or fields[0] != "UNCONN":
            continue
        split = split_host_port(fields[_SS_LOCAL_ADDRESS], ":")
        if split is None:
            continue
        address, port = split
        if port != str(DNS_PORT):
            continue
        if address == "*":
            address = WILDCARD_ADDRESS
        if not is_ipv4(address):
            continue
        pid, process = _parse_ss_process(line)
        listeners.append(Listener(address=address, pid=pid, process=process))
    return tuple(listeners)


_SS_PROCESS = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')


def _parse_ss_process(line: str) -> tuple[int | None, str | None]:
    """Return the first (pid, process) in an ``ss`` users:(...) field.

    The field is absent unless ``ss`` ran as root, so both values are optional
    and their absence is not an error.
    """
    match = _SS_PROCESS.search(line)
    if not match:
        return None, None
    return int(match.group(2)), match.group(1)


def parse_windows_listeners(text: str) -> tuple[Listener, ...]:
    """Return IPv4 port-53 listeners from ``netstat -ano -p UDP`` on Windows.

    Unlike the other two parsers this one has not been validated against output
    from a real Windows host, so callers must treat an empty result from
    non-empty input as "could not interpret" rather than "nothing is listening".
    :func:`parse_failed` expresses that check.
    """
    listeners = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < _WINDOWS_MIN_FIELDS or fields[0].upper() != "UDP":
            continue
        split = split_host_port(fields[_WINDOWS_LOCAL_ADDRESS], ":")
        if split is None:
            continue
        address, port = split
        if port != str(DNS_PORT) or not is_ipv4(address):
            continue
        pid = int(fields[-1]) if fields[-1].isdigit() else None
        listeners.append(Listener(address=address, pid=pid))
    return tuple(listeners)


# Port 53 preceded by either separator and not followed by another digit. The
# trailing guard is what keeps mDNS on 5353 -- present on almost every machine --
# from matching, since ":5353" contains ":53" as a substring.
_MENTIONS_DNS_PORT = re.compile(rf"[:.]{DNS_PORT}(?!\d)")


def parse_failed(raw_output: str, listeners: tuple[Listener, ...]) -> bool:
    """Return whether output mentioning port 53 yielded no parsed listeners.

    Guards against a parser silently reporting "nothing is listening" when it
    simply did not understand the format -- which would send the user to fix the
    wrong thing entirely.
    """
    if listeners:
        return False
    return bool(_MENTIONS_DNS_PORT.search(raw_output))


# ---------------------------------------------------------------------------
# Interface parsing
# ---------------------------------------------------------------------------


def parse_bsd_interfaces(text: str) -> tuple[Interface, ...]:
    """Return interfaces with an IPv4 address from ``ifconfig`` on macOS/BSD."""
    interfaces = []
    current: str | None = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = line.split(":", 1)[0]
            continue
        fields = line.split()
        if current and len(fields) >= _ADDRESS_LINE_MIN_FIELDS and fields[0] == "inet":
            interfaces.append(Interface(name=current, address=fields[1]))
    return tuple(interfaces)


def parse_ip_addr_interfaces(text: str) -> tuple[Interface, ...]:
    """Return interfaces with an IPv4 address from ``ip -4 addr show`` on Linux.

    The interface name is taken from the ``inet`` line's own trailing label
    rather than the enclosing ``N: name:`` header, because an interface with
    several addresses labels each one, and aliases differ from the header.
    """
    interfaces = []
    current: str | None = None
    for line in text.splitlines():
        header = re.match(r"^\d+:\s+([^:@]+)", line)
        if header:
            current = header.group(1).strip()
            continue
        fields = line.split()
        if len(fields) >= _ADDRESS_LINE_MIN_FIELDS and fields[0] == "inet":
            address = fields[1].split("/")[0]
            name = fields[-1] if not fields[-1].startswith("valid_lft") else current
            interfaces.append(Interface(name=name or "", address=address))
    return tuple(interfaces)


def find_shared_interface(
    interfaces: tuple[Interface, ...], name_patterns: tuple[str, ...]
) -> Interface | None:
    """Return the interface most likely to be the USB sharing link.

    Matches on name prefix, then requires a usable IPv4 address. Loopback is
    excluded because a resolver bound only to loopback is exactly the failure
    being diagnosed, not a candidate for the shared link.
    """
    for interface in interfaces:
        if not is_ipv4(interface.address) or interface.address.startswith("127."):
            continue
        if any(interface.name.startswith(prefix) for prefix in name_patterns):
            return interface
    return None


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


def diagnose(shared: Interface | None, listeners: tuple[Listener, ...]) -> Diagnosis:
    """Return the state of DNS service on the shared link.

    A wildcard listener counts as covering the shared address: a resolver bound
    to ``0.0.0.0`` answers on every interface including ones created after it
    started, which is precisely the configuration that does not exhibit the
    fault.
    """
    if shared is None:
        return Diagnosis.NO_SHARED_INTERFACE
    if not listeners:
        return Diagnosis.NO_RESOLVER
    covered = any(listener.address in (shared.address, WILDCARD_ADDRESS) for listener in listeners)
    if covered:
        return Diagnosis.HEALTHY
    return Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE

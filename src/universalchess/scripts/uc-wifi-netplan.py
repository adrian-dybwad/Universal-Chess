#!/usr/bin/env python3
"""Write netplan Wi-Fi when NetworkManager is not installed.

``uc-wifi-admin`` calls this as root on Armbian (networkd + wpa_supplicant).
Raspberry Pi OS still uses nmcli in the helper and never reaches this file.
The passphrase arrives on stdin, never as an argument.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404  # fixed-argv `netplan apply` only, never shell=True
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

WIFI_NETPLAN_NAME = "60-universal-chess-wifi.yaml"
_IFACE_RE = re.compile(r"^wlan[0-9]+$")
_AP_KEY_RE = re.compile(r'^\s+"((?:\\.|[^"\\])*)":', re.MULTILINE)


def yaml_quote(value: str) -> str:
    """Double-quote a YAML scalar, escaping backslash and quote."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_unquote(value: str) -> str:
    """Undo :func:`yaml_quote` escaping."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


def render_wifi_yaml(iface: str, ssid: str, password: str) -> str:
    """Return a complete netplan document for one Wi-Fi AP on ``iface``."""
    quoted_ssid = yaml_quote(ssid)
    if password:
        ap_block = f"        {quoted_ssid}:\n          password: {yaml_quote(password)}\n"
    else:
        ap_block = f"        {quoted_ssid}: {{}}\n"
    return (
        "# Written by Universal Chess (uc-wifi-netplan.py). Replaced on each Connect.\n"
        "network:\n"
        "  version: 2\n"
        "  renderer: networkd\n"
        "  wifis:\n"
        f"    {iface}:\n"
        "      dhcp4: true\n"
        "      optional: true\n"
        "      access-points:\n"
        f"{ap_block}"
    )


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace ``path`` without leaving a truncated file on power loss."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-uc-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def disable_sibling_wifi_yaml(netplan_dir: Path, keep_name: str) -> None:
    """Rename other ``*.yaml`` files that define ``wifis:`` so they cannot conflict.

    usb0 Client DHCP files have ``ethernets:`` only and are left alone.
    """
    for path in netplan_dir.glob("*.yaml"):
        if path.name == keep_name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            # Running as root, so an unreadable entry here is a dangling symlink
            # or a file that vanished between the glob and the read -- neither
            # is a live config that could claim the radio. Left in place rather
            # than moved aside, because a file that cannot be inspected must not
            # be renamed on the guess that it defines wifis:. Reported so a
            # connect that fails anyway has something to point at.
            print(f"uc-wifi-netplan: cannot read {path}: {exc}", file=sys.stderr)
            continue
        if re.search(r"^\s*wifis:", text, re.MULTILINE) is None:
            continue
        off = path.with_name(path.name + ".uc-wifi-off")
        path.replace(off)


def apply_netplan() -> int:
    """Run ``netplan apply``. Returns the process exit status."""
    netplan = shutil.which("netplan")
    if netplan is None:
        for root in ("/usr/sbin", "/sbin"):
            candidate = Path(root) / "netplan"
            if os.access(candidate, os.X_OK):
                netplan = str(candidate)
                break
    if netplan is None:
        print("uc-wifi-netplan: netplan not found", file=sys.stderr)
        return 1
    result = subprocess.run([netplan, "apply"], check=False)  # noqa: S603  # nosec B603
    return int(result.returncode)


def connect_wifi(
    iface: str,
    netplan_dir: Path,
    ssid: str,
    password: str,
    apply: Optional[Callable[[], int]] = None,
) -> int:
    """Write the Wi-Fi netplan file and apply it."""
    if _IFACE_RE.fullmatch(iface) is None:
        print(f"uc-wifi-netplan: invalid interface {iface!r}", file=sys.stderr)
        return 2
    if not ssid or "\n" in ssid:
        print("uc-wifi-netplan: invalid SSID", file=sys.stderr)
        return 2
    if "\n" in password:
        print("uc-wifi-netplan: invalid passphrase", file=sys.stderr)
        return 2
    netplan_dir = Path(netplan_dir)
    disable_sibling_wifi_yaml(netplan_dir, WIFI_NETPLAN_NAME)
    write_atomic(netplan_dir / WIFI_NETPLAN_NAME, render_wifi_yaml(iface, ssid, password))
    runner = apply if apply is not None else apply_netplan
    return runner()


def saved_ssids(netplan_dir: Path) -> List[str]:
    """Return SSIDs configured in the Universal Chess Wi-Fi netplan file."""
    path = Path(netplan_dir) / WIFI_NETPLAN_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [yaml_unquote(match.group(1)) for match in _AP_KEY_RE.finditer(text)]


def forget_wifi(
    netplan_dir: Path,
    ssid: str,
    apply: Optional[Callable[[], int]] = None,
) -> int:
    """Remove the Universal Chess Wi-Fi file if it names ``ssid``."""
    if ssid not in saved_ssids(netplan_dir):
        return 1
    path = Path(netplan_dir) / WIFI_NETPLAN_NAME
    try:
        path.unlink()
    except OSError as exc:
        print(f"uc-wifi-netplan: cannot remove {path}: {exc}", file=sys.stderr)
        return 1
    runner = apply if apply is not None else apply_netplan
    return runner()


def main(argv: List[str]) -> int:
    """Dispatch connect / forget / saved. Unknown argv is a usage error."""
    if not argv:
        return 2
    verb = argv[0]
    if verb == "connect" and len(argv) == 4:
        password = sys.stdin.read().rstrip("\n")
        return connect_wifi(argv[1], Path(argv[2]), argv[3], password)
    if verb == "forget" and len(argv) == 4:
        return forget_wifi(Path(argv[2]), argv[3])
    if verb == "saved" and len(argv) == 2:
        for ssid in saved_ssids(Path(argv[1])):
            print(ssid)
        return 0
    print(
        "usage: uc-wifi-netplan.py connect <iface> <netplan-dir> <ssid>\n"
        "       uc-wifi-netplan.py forget <iface> <netplan-dir> <ssid>\n"
        "       uc-wifi-netplan.py saved <netplan-dir>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

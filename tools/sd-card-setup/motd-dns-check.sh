#!/bin/sh
# Explain a dead host DNS forwarder at login.
#
# In rpi-usb-gadget's client mode this board takes its nameserver by DHCP from
# the computer on the other end of the USB cable. A host that advertises itself
# as a DNS server without running one produces a failure that tells the user
# nothing: routing works, numeric addresses work, and every lookup dies with
# "Temporary failure in name resolution" while apt fails for no visible reason.
#
# This script only reports. It configures nothing on purpose. Pointing the
# profile at a public resolver was the obvious alternative and was rejected:
# NetworkManager orders connection-configured servers ahead of the DHCP-supplied
# one, so it does not act as a fallback -- it silently becomes the primary
# resolver for every lookup, on every network, permanently.
#
# Installed to /etc/update-motd.d/ by the SD card setup tool, alongside Raspberry
# Pi's own 99-rpi-usb-gadget hint, and run by pam_motd at login.

RESOLV_CONF="${UC_RESOLV_CONF:-/etc/resolv.conf}"
PROBE_NAME="${UC_PROBE_NAME:-deb.debian.org}"
PROBE_TIMEOUT="${UC_PROBE_TIMEOUT:-2}"

nameserver=$(awk '/^nameserver /{print $2; exit}' "$RESOLV_CONF" 2>/dev/null)

# Nothing is configured to answer, so there is no host to point the user at.
# The cause is local and this script's explanation would be wrong.
[ -n "$nameserver" ] || exit 0

# No default route means the link itself never came up -- the host is not
# sharing its connection. Lookups fail for that reason, and blaming DNS would
# send the user to restart a forwarder when the fix is enabling Internet
# Sharing.
[ -n "$(ip route show default 2>/dev/null)" ] || exit 0

# The bound is what keeps a healthy login fast: this exits immediately on
# success, and only a genuinely broken resolver costs the timeout. Where
# `timeout` is unavailable the lookup still runs unbounded rather than being
# treated as a failure -- reporting broken DNS because a helper is missing
# would put a permanent false warning on every login.
if command -v timeout >/dev/null 2>&1; then
    timeout "$PROBE_TIMEOUT" getent hosts "$PROBE_NAME" >/dev/null 2>&1 && exit 0
else
    getent hosts "$PROBE_NAME" >/dev/null 2>&1 && exit 0
fi

echo
echo "Universal Chess: name resolution is not working"
echo
echo "  This board uses $nameserver for DNS, learned by DHCP from the computer"
echo "  on the other end of the USB cable. That server is not replying, so apt"
echo "  and any download will fail."
echo
echo "  A default route is present, so the link itself is up and this is very"
echo "  likely DNS alone rather than a dead connection."
echo
echo "  Fix this on the host, not here -- nothing on this board is misconfigured."
echo "  The usual cause is a resolver that started before the USB interface"
echo "  existed and so never bound to it. On the host, run:"
echo
echo "      python3 enable_usb_gadget.py --check-dns --fix"
echo
echo "  That names the process holding port 53 and, where the remedy is known,"
echo "  offers to restart it. To check by hand instead:"
echo
echo "      macOS, Linux   sudo lsof -nP -iUDP:53"
echo "      Windows        netstat -ano -p UDP | findstr :53"
echo

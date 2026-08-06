# SD card setup: USB Ethernet gadget

Prepares a freshly imaged Raspberry Pi SD card so the board is reachable from a
host computer over a single USB cable, before it has any network at all.

Run this on your own machine, after Raspberry Pi Imager has written the card and
**before** the card's first boot.

```bash
python3 tools/sd-card-setup/enable_usb_gadget.py --dry-run   # show the changes
python3 tools/sd-card-setup/enable_usb_gadget.py             # apply them
```

The card is auto-detected. Pass `--boot /path/to/bootfs` if you have more than
one card mounted, or if detection fails.

A detected card is described and confirmed before anything is written, so you
can tell it apart from any other volume that happens to be mounted:

```
Found this card:
  Card      /Volumes/bootfs
  Image     Raspberry Pi reference 2025-05-13
  Written   2026-08-05 14:11
  Hostname  dgtcentaur.local
  Account   pa
  Size      510 MB, 434 MB free

  (the size is the boot partition, not the whole card)

Is this the card you want to prepare? [y/N]
```

Anything that cannot be read is left out rather than filled in with a
placeholder — a card imaged without Imager customisation has no hostname or
account to show, and inventing one would defeat the point of showing it. A card
named explicitly with `--boot` is described but not queried, since naming it was
already the decision this prompt asks for.

One run covers the whole job. After writing the card it asks you to move it to
the Pi and connect the cable, waits for the board to come up, and then checks
that DNS works over the new link — offering to fix it if it does not. There is
no second tool to find, and nothing to remember to run afterwards.

```
Done. Originals saved alongside as *.uc-orig.
...
Turn on internet connection sharing for the USB interface first: the
Pi needs a route out, and on macOS the interface watched for below is
the bridge that Internet Sharing creates.
Then eject the card, put it in the Pi, and connect the USB cable.
Wait for the board and check DNS? [Y/n]

Waiting up to 300s for the macOS end of the link to appear. A Pi Zero's
first boot is slow; Ctrl-C to stop.
Link is up: bridge100 at 192.168.2.1

Diagnosis: healthy
```

The wait only happens when you are sitting at a terminal. Scripted runs, and
runs with `--no-wait`, stop once the card is written. If the board never turns
up, the card is still correctly prepared; the tool says so and exits cleanly
rather than reporting a failure that did not occur.

## Why this exists

Universal Chess installs over the network, so a board that cannot join Wi-Fi
cannot be set up at all — and you cannot enable a fallback on a board you cannot
reach. That has to be solved on the card, before first boot.

It is also the recovery path. If Wi-Fi later breaks, moves house, or the router
changes, a USB cable still gets you in.

## What it changes

Only the FAT boot partition is touched. That is deliberate: it is the only
partition readable from Windows and macOS, since the root filesystem is ext4.

| File | Change |
| --- | --- |
| `config.txt` | dwc2 overlay in peripheral mode, under an explicit `[all]` filter |
| `cmdline.txt` | `modules-load=dwc2,g_ether` |
| `user-data` | a cloud-init `runcmd` that invokes `rpi-usb-gadget`, and a `write_files` entry installing the DNS diagnostic below |
| `ssh` | created empty, which turns on sshd at first boot |

No DNS server is configured on the card, and no network setting is changed
beyond bringing the gadget up. See [section 6](#6-dns-fails-while-numeric-addresses-work)
for why that is a deliberate choice rather than an omission.

Putting the module load on the kernel command line rather than in
`/etc/modules-load.d` means the gadget is live on the **first** boot, with no
reboot — and it does not need the root filesystem, which we cannot write to.

## Why not `rpi: enable_usb_gadget: true`

Raspberry Pi documents a cloud-init key for this, and it is the obvious choice.
It does not work on the slower Zero boards.

That key routes through `cc_raspberry_pi.configure_usb_gadget`, which runs
`rpi-usb-gadget on -f` under a **15-second timeout**. The script takes about 19
seconds on a Zero 2 W because of the `nmcli` settle waits it performs. The
timeout expires, the module records a failure and skips the reboot it would
normally trigger, and gadget mode never comes up. Upstream cloud-init has since
raised the timeout to 30 seconds, but the version shipped in current Raspberry Pi
OS images (`25.2-1~bpo13+1+rpt20`) still has 15.

An original Pi Zero is slower still — a single-core ARM1176 against the Zero 2
W's quad-core A53 — so it has even less chance of finishing inside the timeout.

A `runcmd` carries no such timeout, so this tool uses one. If your card already
sets the `rpi:` key, leaving it is harmless — the `runcmd` runs later and fixes
it up. The tool tells you when it sees this.

## Reaching the board

`rpi-usb-gadget` ships two NetworkManager profiles, and `rpi-usb-gadget on` —
what this tool triggers — activates the **client** one:

| Mode | Pi's address | Who runs DHCP | Pi gets internet |
| --- | --- | --- | --- |
| `client` (default) | leased by the host | the host | yes, via the host |
| `shared` | `10.12.194.1` | the Pi | no |

In client mode the Pi has **no fixed address**, so reach it by name:

- `http://<hostname>.local/` — needs mDNS on the host. Built in on macOS and
  most Linux; on Windows it needs Bonjour.

If mDNS does not resolve, find the address the host leased: `arp -an | grep
bridge` or `/var/db/dhcpd_leases` on macOS, `arp -a` on Windows, `ip neigh show
dev <usb interface>` on Linux.

Client mode requires the host to share its connection over the USB interface —
Internet Sharing on macOS, ICS on Windows, or NetworkManager's "Shared to other
computers" on Linux. Without it the Pi gets no address at all, which looks
exactly like a gadget that failed to come up.

To skip host-side configuration, run `sudo rpi-usb-gadget shared` on the Pi. It
then serves `10.12.194.1` and its own DHCP, but has no route to the internet —
so it cannot install packages.

**Windows** needs a one-time RNDIS driver, from
[rpi-usb-gadget releases](https://github.com/raspberrypi/rpi-usb-gadget/releases).
macOS and Linux need nothing — they bind CDC-ECM natively.

**Use the right port.** On a Pi Zero this is the micro-USB socket next to the
mini-HDMI, *not* the one marked `PWR IN`. Only that port supports device mode.

## Getting the board online to install

Installing Universal Chess pulls packages from apt, so the board needs a route
to the internet and working name resolution.

In client mode the cable provides both, *provided the host shares its
connection*. Verify from the Pi before starting an install:

```bash
ping -c1 8.8.8.8        # route and NAT
getent hosts deb.debian.org   # name resolution
```

Both must succeed. If the first works and the second does not, see
[DNS fails while numeric addresses work](#6-dns-fails-while-numeric-addresses-work).

On a board that *has* Wi-Fi, joining it from the USB shell is an alternative
that removes the dependency on host sharing:

```bash
sudo nmcli device wifi connect "YourSSID" --ask
```

An original Pi Zero (non-W) has no Wi-Fi at all, so for that board the USB cable
is the only path and host sharing is not optional.

## Troubleshooting

Work down these in order. Each step assumes the ones above it passed, and each
failure has a different cause — skipping ahead usually means fixing the wrong
thing. The example values come from a real Pi Zero on a macOS host.

### 1. The host does not see a USB device

```bash
# macOS
system_profiler SPUSBDataType | grep -A3 "USB Gadget"
networksetup -listallhardwareports | grep -A2 "Raspberry Pi USB Gadget"
# Linux
lsusb | grep -i raspberry
# Windows: Device Manager > Network adapters > "USB Ethernet/RNDIS Gadget"
```

Good looks like `Vendor ID: 0x2e8a  Manufacturer: Raspberry Pi Ltd.` and a
hardware port named `Raspberry Pi USB Gadget` mapped to a device such as `en21`.

Nothing at all means the gadget never came up. In order of likelihood: the cable
is in the **wrong port** (on a Zero, use the micro-USB next to the mini-HDMI,
not `PWR IN`); the cable is charge-only with no data lines; or the card edits
did not apply — re-run the tool with `--dry-run` against the card, which reports
"nothing to do" on a card that is already prepared.

### 2. The device appears but the link is down

```bash
ifconfig en21 | grep -E 'status|media'     # macOS, substitute your device
ip -br link show usb0                      # on the Pi
```

Expect `status: active` and `media: autoselect (100baseTX <full-duplex>)`.

During the **first boot** the link legitimately flaps: cloud-init is running and
`rpi-usb-gadget` tears the interface down and reconfigures it. Ping loss and
stalled SSH handshakes in the first few minutes are expected. Give it time
before treating this as a fault.

### 3. The link is up but the Pi has no address

This is almost always the host, not the Pi. Client mode needs the host to run a
DHCP server on the USB interface.

```bash
# macOS: Internet Sharing creates bridge100 and puts the USB device in it
ifconfig bridge100 | grep -E 'inet |member'
```

Good looks like `inet 192.168.2.1` and `member: en21`. If `bridge100` does not
exist, enable **System Settings > General > Sharing > Internet Sharing**, share
from your internet connection *to* the Raspberry Pi USB Gadget. On Windows use
the Sharing tab of the upstream adapter (ICS); on Linux set the usb0 connection
to "Shared to other computers".

### 4. The Pi has an address but you cannot find it

The Pi has **no fixed IP** in client mode, so do not go looking for
`10.12.194.1` — that address only exists in `shared` mode.

```bash
# by name (preferred)
ping <hostname>.local
dscacheutil -q host -a name <hostname>.local      # macOS

# by lease, if mDNS does not resolve
arp -an | grep bridge                             # macOS
cat /var/db/dhcpd_leases                          # macOS
arp -a                                            # Windows
ip neigh show dev <usb interface>                 # Linux
```

mDNS needs `avahi-daemon` running on the Pi (`systemctl is-active avahi-daemon`)
and Bonjour on a Windows host.

### 5. SSH says "Host key verification failed"

Not a connectivity problem. The host key was recorded under a different name for
the same board — typically the IP first, then the `.local` name. Remove the old
entry:

```bash
ssh-keygen -R <hostname>.local
ssh-keygen -R <ip address>
```

### 6. DNS fails while numeric addresses work

**The board diagnoses this one itself.** Cards prepared by this tool carry a
check in `/etc/update-motd.d/`, so if the fault is present you are told at login
rather than having to work through this section:

```
Universal Chess: name resolution is not working

  This board uses 192.168.2.1 for DNS, learned by DHCP from the computer
  on the other end of the USB cable. That server is not replying, so apt
  and any download will fail.
  ...
```

It prints nothing when resolution works, stays quiet when there is no default
route (a down link is section 3, not this), and costs no measurable login time.
The source is `motd-dns-check.sh` in this directory, installed verbatim.

The rest of this section is the manual diagnosis behind that message.

The signature is a board that is plainly online but cannot resolve anything:

```bash
ping -c1 8.8.8.8      # succeeds
ping -c1 google.com   # "Temporary failure in name resolution"
```

The host advertises itself as the Pi's DNS server over DHCP. The Pi believes it
and asks; if nothing is listening there, every lookup times out while routing
and NAT keep working perfectly. Confirm the Pi is configured as expected:

```bash
cat /etc/resolv.conf                        # nameserver = the host's address
nmcli dev show usb0 | grep IP4.DNS
```

Raspberry Pi OS Lite ships no `dig` or `nslookup`, so query specific servers
directly to prove where the fault is:

```bash
python3 - <<'EOF'
import socket, struct

def query(server, name):
    pkt = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    for part in name.split("."):
        pkt += bytes([len(part)]) + part.encode()
    pkt += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(512)
        return "REPLY rcode=%d answers=%d" % (
            data[3] & 0xF, struct.unpack(">H", data[6:8])[0])
    except Exception as exc:
        return "NO REPLY (%s)" % type(exc).__name__
    finally:
        s.close()

for server in ("192.168.2.1", "8.8.8.8", "1.1.1.1"):   # host, then public
    print("%-14s %s" % (server, query(server, "google.com")))
EOF
```

The host failing while both public resolvers answer isolates the fault to the
host's forwarder.

#### The host's resolver never bound the USB interface

This is the common case, and the fix is on the host.

The tool already performs this check at the end of its run, once the board is
connected, so a card prepared in the normal way needs nothing extra. `--check-dns`
runs the same check on its own, for a board already in service — which is what
the Pi's login banner points at, since by then the card was prepared long ago.
Run it on the **host**, with the Pi connected:

```bash
python3 enable_usb_gadget.py --check-dns          # diagnose only
python3 enable_usb_gadget.py --check-dns --fix    # offer to repair
```

It never looks for a card in this mode, so it is safe to run with no reader
attached.

It finds the shared link, reports what is listening on port 53, and names the
diagnosis. Without `--fix` it changes nothing. With it, on macOS, it identifies
the owning process and offers to restart it — after checking the process is
supervised, because restarting a resolver nothing relaunches would leave the
host with no DNS at all. On Linux and Windows it prints the command instead of
running it, since no repair has been verified there.

Both commands share one implementation: `hostdns.py` reduces command output to a
diagnosis and `hostcheck.py` runs the commands and applies the remedy, so the
check cannot drift between the two entry points.

The rest of this section is the same diagnosis performed by hand. Check which
addresses have something listening on port 53:

```bash
netstat -an -f inet -p udp | awk '$4 ~ /\.53$/ {print}'
```

Healthy output includes the bridge address, `192.168.2.1.53`. The fault looks
like this instead — note that `192.168.14.106` is an address the Mac no longer
has, left over from a network it was on days earlier:

```
udp4  0  0  127.0.0.1.53         *.*
udp4  0  0  192.168.14.106.53    *.*
```

Identify the process that owns those sockets before restarting anything. It is
not necessarily the one you expect:

```bash
sudo lsof -nP -iUDP:53
```

In the case that produced the output above it was **Homebrew `dnsmasq`**, not
macOS's own `mDNSResponder`. Restarting it fixed the problem immediately:

```bash
sudo killall dnsmasq        # launchd relaunches it; it then binds 192.168.2.1
```

Where macOS's own forwarder is the owner, the equivalent is
`sudo killall mDNSResponder`, which `launchd` likewise respawns.

##### Why a restart is what fixes it

dnsmasq enumerates interfaces once at startup and binds one socket per address
it finds. That set is fixed for the life of the process — the mode that tracks
interfaces appearing and disappearing, `bind-dynamic`, is Linux-only, and the
macOS build reports `no-inotify`. There is no renewal to fail; a restart is the
only thing that re-enumerates.

`bridge100` exists only once Internet Sharing has an interface to share to, and
its member is the USB interface, which appears when the Pi enumerates. Any
resolver running with `RunAtLoad` therefore starts long before that bridge, and
will always miss it. **Expect this to recur on later connections**, with the
same one-line fix.

Two things that sound plausible and do not work, both verified:

- **Toggling Internet Sharing off and on.** It restarts `bootpd` and the NAT
  rules but does not touch the resolver's sockets, and the owning process keeps
  the same PID across the toggle.
- **`sudo killall -HUP mDNSResponder`.** SIGHUP makes it reload configuration
  without rebinding, and the process does not actually restart — its uptime is
  unchanged afterwards. It is also the wrong process whenever something else
  owns port 53.

Verify end to end from the Pi:

```bash
getent hosts deb.debian.org
```

#### Making the Pi independent of the host's forwarder

If the host cannot be fixed, point the Pi at public resolvers:

```bash
sudo nmcli con mod "USB Gadget (client)" \
  ipv4.dns "1.1.1.1 8.8.8.8" ipv4.dns-options "timeout:1 attempts:1"
sudo nmcli dev reapply usb0
```

`dev reapply` pushes DNS changes without deactivating the connection, so an SSH
session over the gadget survives it. `nmcli con up` also works but drops the
link and therefore your session.

`ipv4.ignore-auto-dns` is deliberately **not** set here, and would make no
practical difference: NetworkManager already places connection-configured
servers ahead of the DHCP-supplied one. Verified on a Pi Zero, the result is

```
nameserver 1.1.1.1
nameserver 8.8.8.8
nameserver 192.168.2.1
options timeout:1 attempts:1
```

**Understand what this does before leaving it in place.** It does not add a
fallback — it *replaces* the host as the primary resolver, because the public
servers are consulted first and the host's server is only reached if both fail.
Every lookup from the board then goes to Cloudflare and Google, including on
networks where the host's resolver works fine, and any name the host resolves
privately (split-horizon or local-only records) stops resolving. `timeout:1`
keeps the reverse case cheap: where a public resolver is firewalled off, each
lookup falls through to the host's server after about a second.

Treat this as a targeted workaround for a broken host, not a default. Undo it
with:

```bash
sudo nmcli con mod "USB Gadget (client)" ipv4.dns "" ipv4.dns-options ""
sudo nmcli dev reapply usb0
```

#### Why the tool does not set this up for you

Baking these servers into the card was considered and rejected on the strength
of the output above. Because manual servers take precedence, there is no way to
express "use the host, and fall back to a public resolver" through
`ipv4.dns` — configuring it makes the public resolver the primary for every
lookup on every board, forever, whether or not the host's DNS was ever broken.
That is a DNS leak shipped to users who never asked for one, and it breaks hosts
doing split-horizon resolution.

Preserving the correct precedence needs a NetworkManager dispatcher script that
probes the host's server on each link event and adds or removes a fallback
accordingly. That is a self-modifying network configuration on every board, with
its own failure modes, to work around a fault that lives entirely on the host.
The one case investigated in detail turned out to be a third-party `dnsmasq` on
a macOS host that had bound its sockets before the USB bridge existed — nothing
the Pi could have detected as different from any other dead resolver, and
nothing a card-side setting should be papering over.

So the card configures no DNS at all. It installs the login-time check described
at the top of this section instead, which turns the unhelpful "Temporary failure
in name resolution" into a message naming the server at fault and the fix, and
leaves the choice of workaround to the person reading it.

### 7. The web UI does not answer on port 80 or 443

Check the board is actually running Universal Chess before debugging the
network. A freshly imaged card has nothing installed, so the port is refused
rather than timing out — a refusal means you reached the Pi and nothing was
listening, which is a different problem from no route.

```bash
systemctl status universal-chess
```

## Layout

Users download **one file**. This directory is the source it is built from.

| File | Kind | Role |
| --- | --- | --- |
| `enable_usb_gadget.py` | entry point | the whole tool: prepares the card, waits, checks DNS |
| `console.py` | library | output and prompts |
| `bootfs.py` | library | pure text transformations of the boot partition files |
| `hostdns.py` | library | pure parsing and diagnosis of host DNS state |
| `hostcheck.py` | library | runs host commands, waits for the link, applies the remedy |
| `motd-dns-check.sh` | installed on the Pi | reports a broken resolver at login |
| `build_single_file.py` | build | merges the above into the file users download |

The split exists for testing, not for users: the pure halves in `bootfs.py` and
`hostdns.py` are what let the suite cover every failure mode with no card, no
Pi, and no real clock. `build_single_file.py` concatenates them in dependency
order and binds each module name to the merged module, so a call such as
`bootfs.parse_cloud_config(...)` keeps resolving without rewriting call sites.

That merge is only sound while no two modules define the same top-level name, so
the build refuses to run when they do. Letting one definition quietly win would
produce a file that runs and misbehaves, and no test of the modular sources
could catch it, because there the two names are genuinely distinct.

```bash
python3 tools/sd-card-setup/build_single_file.py --check    # verify only
python3 tools/sd-card-setup/build_single_file.py            # write the file
```

Nothing generated is committed. CI builds it during a release and attaches it,
so there is no checked-in copy that can fall out of step with the sources.

## Options

| Flag | Effect |
| --- | --- |
| `--boot PATH` | Use this boot partition instead of auto-detecting |
| `--dry-run` | Show the diff and exit without writing |
| `--yes` | Skip the confirmation prompts |
| `--no-ssh` | Do not create the `ssh` marker file |
| `--free-uart` | Also detach the kernel serial console |
| `--no-wait` | Stop after writing the card; skip the board wait and DNS check |
| `--wait` | Wait for the board even with no terminal attached |
| `--wait-timeout SECONDS` | How long to wait for the board (default 300) |
| `--check-dns` | Skip the card; only check DNS on an already-connected board |
| `--fix` | With `--check-dns`, offer to restart a resolver that missed the link |
| `--shared-address ADDR` | With `--check-dns`, use this address instead of detecting it |

Waiting is on by default only when a terminal is attached, because a run with
nobody there to plug a board in would otherwise stall for minutes on hardware
that will never arrive. `--wait` forces it for deliberate automation.

`--free-uart` is for DGT Centaur hardware, where the board speaks on the same
UART the kernel console uses. It is unrelated to USB access and off by default.

Use `--shared-address` when the host shares over an interface the tool does not
recognise by name. It reports the address it selected, so a wrong guess is
visible rather than silent.

## Safety

The tool refuses to run against anything that is not a Raspberry Pi boot
partition, describes an auto-detected card and asks before using it, shows the
full diff and asks again before writing, and backs each modified file up once to
`<name>.uc-orig`. Writes are fsynced, so the card is safe to eject as soon as it
finishes. Re-running on a prepared card reports that there is nothing to do.

## Requirements

Python 3.9 or newer. PyYAML is optional — when present, the edited `user-data` is
parsed to confirm it is still valid before anything is written. Without it the
edit still happens and the tool says validation was skipped.

## Caveats

Enabling peripheral mode **disables the Pi's USB host port**. Nothing in
Universal Chess uses it, but you cannot attach USB peripherals to a Zero while
gadget mode is on.

**Take the Pi out of the Centaur before plugging it into a computer.** The Pi's
OTG port can also source power, and in the Centaur the Pi is powered from the
board, so connecting to a PC may backfeed the board's 5V rail. It may well be
fine — but connect both at your own risk, as with all modding of this game. Do
the whole setup with the Pi on the desk and refit it at the end.

## Tests

```bash
.venv/bin/python -m pytest tools/sd-card-setup/tests -q
```

The transformations in `bootfs.py` are pure text-to-text functions with no
filesystem access, so every failure mode — a two-line `cmdline.txt`, an overlay
landing in the wrong conditional section, a duplicated cloud-init key — is
covered without needing a card.

The host half is testable for the same reason: `hostdns.py` parses captured
`netstat`, `ss` and `ifconfig` output, and `hostcheck.py` takes its command
runner, clock and sleep as arguments. The wait loop is exercised over a fake
clock, so the suite covers a board that appears late and one that never appears
without spending real time on either.

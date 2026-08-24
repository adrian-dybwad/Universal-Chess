## Prepare a new SD card

Follow the [install procedure](https://github.com/adrian-dybwad/Universal-Chess/blob/main/docs/install.md)
to prepare a new SD card before installing.

## Installation

Download and install on your Raspberry Pi:

```bash
# Download into a fresh temp directory (run on the Pi). Every nightly ships
# under the same filename, and wget will not overwrite an existing file -- it
# saves to "...deb.1" instead -- so downloading into a directory that already
# holds a previous nightly leaves the stale copy under the name apt installs.
# /var/tmp is used because /tmp is a small RAM-backed tmpfs on Trixie.
UC_DEB="$(mktemp -d -p /var/tmp)/universal-chess___NIGHTLY_VERSION___all.deb"
wget -O "$UC_DEB" https://github.com/__REPOSITORY__/releases/download/__TAG_NAME__/universal-chess___NIGHTLY_VERSION___all.deb

# Install -- apt resolves dependencies in one step.
# --reinstall is needed because every nightly shares the version "__NIGHTLY_VERSION__".
sudo apt-get install -y --reinstall "$UC_DEB"
rm -f "$UC_DEB"
```

Or download the `.deb` file from the Assets section below and transfer it to your Pi.

When the install finishes, reboot the Pi and it should come up running
Universal Chess.

### No Wi-Fi? Install over the USB cable

If the Pi cannot join your Wi-Fi it never appears on the network, and with no
screen or keyboard there is nothing to log into to fix it. `enable_usb_gadget.py`
in the Assets below prepares the card **before its first boot** so the Pi appears
as a USB Ethernet adapter, and one cable gives you a login plus internet access.

> **IMPORTANT: take the Pi out of the Centaur before you plug it into a
> computer.** In the Centaur the Pi is powered by the board, and it may not like
> being powered by the board and by USB at the same time. It may well be fine —
> but connect both at your own risk, as with all modding of this game.

Image the card with Raspberry Pi OS **Trixie** Lite, 32-bit — that is the
combination this was tested on; 64-bit may work too. Then run the tool on the
computer that imaged the card, not on the Pi. It is one self-contained file and
needs only Python 3.9 or newer.

The Imager ejects the card when it finishes, so take it out of the reader and
put it back before running the tool, otherwise the computer cannot see it.

On macOS and Linux:

```bash
curl -LO https://github.com/__REPOSITORY__/releases/download/__TAG_NAME__/enable_usb_gadget.py
python3 enable_usb_gadget.py
```

On Windows, download `enable_usb_gadget.py` from the Assets section below, then
run `py enable_usb_gadget.py` in PowerShell.

The card is detected automatically; the tool describes it and asks before
writing.

Next, turn on internet connection sharing — Internet Sharing on macOS, the
Sharing tab on Windows (which also needs a one-time
[RNDIS driver](https://github.com/raspberrypi/rpi-usb-gadget/releases)), or
*Shared to other computers* on the `usb0` connection on Linux. Do this before
connecting the Pi: without it the Pi has a login but no route out, and `apt`
fails.

Then move the card to the Pi and connect the cable — the **middle** micro-USB
port on a Pi Zero, not `PWR IN`. The tool waits for the board and checks name
resolution over the new link.

**Be patient here.** It takes several minutes for the board to become reachable,
and longer on a plain Pi Zero. A missing device, a flapping link and a hanging
SSH are all normal while cloud-init does its first-boot work.

Finally SSH in as `<your-user>@<your-hostname>.local`, run
`sudo apt update && sudo apt upgrade -y`, and follow the install commands above.
Once it is running, open `http://<your-hostname>.local/` to add any engines you
want, then shut the Pi down and refit it in the Centaur.

Full instructions, including per-platform troubleshooting, are in the
[install procedure](https://github.com/adrian-dybwad/Universal-Chess/blob/main/docs/install.md#no-wi-fi-set-the-board-up-over-the-usb-cable).

## Confirm which build is installed

Every nightly carries the package version `__NIGHTLY_VERSION__`, so `dpkg -l`
and apt cannot tell one build from another. The installed build is identified
by its release tag:

```bash
cat /opt/universalchess/VERSION
```

This should print `__TAG_NAME__`. Anything else means an older `.deb` was
installed.

## Switch to Stable

To switch back to stable releases:
```bash
sudo apt-get install --reinstall universal-chess
```

## Prepare a new SD card

Start clean on a new SD card using the Raspberry Pi Imager. Do not reuse
the original card from the Centaur -- keep it safe, you may want it later.

1. In the Pi Imager, choose Raspberry Pi OS (32-bit) **Trixie**, with no
   apps or desktop (Lite).
2. In the Imager's advanced settings, configure the hostname, a user and
   password (any username works, you are not limited to `pi` -- just
   remember the password), Wi-Fi credentials, and enable SSH. Set any
   other options you like.
3. Write the image, insert the card into the Pi, and power it on. With
   Wi-Fi configured correctly the Pi usually joins your network within a
   minute or two. No screen or keyboard required.
4. SSH into the Pi, then update the base system:

```bash
sudo apt update && sudo apt upgrade -y
```

## Installation

Download the `.deb` file and install on your Raspberry Pi:

```bash
# apt resolves dependencies in one step (the ./ prefix is required)
sudo apt-get install -y ./universal-chess___VERSION___all.deb
```

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
combination this was tested on; 64-bit may work too. The Imager ejects the card
when it finishes, so take it out of the reader and put it back before running
the tool, otherwise the computer cannot see it.

Download the tool and run it on the computer that imaged the card — not on the
Pi. It is one self-contained file and needs only Python 3.9 or newer:

```bash
python3 enable_usb_gadget.py    # macOS and Linux
py enable_usb_gadget.py         # Windows, in PowerShell
```

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
`sudo apt update && sudo apt upgrade -y`, and follow the install command above.
Once it is running, open `http://<your-hostname>.local/` to add any engines you
want, then shut the Pi down and refit it in the Centaur.

Full instructions, including per-platform troubleshooting, are in the
[install procedure](https://github.com/adrian-dybwad/Universal-Chess/blob/main/docs/install.md#no-wi-fi-set-the-board-up-over-the-usb-cable).

## Verify Download

```bash
sha256sum -c SHA256SUMS.txt
```

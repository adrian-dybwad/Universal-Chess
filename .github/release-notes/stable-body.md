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

Download it and run it on the computer that imaged the card — not on the Pi —
with the card still in the reader. It is one self-contained file and needs only
Python 3.9 or newer:

```bash
python3 enable_usb_gadget.py    # macOS and Linux
py enable_usb_gadget.py         # Windows, in PowerShell
```

The card is detected automatically; the tool describes it and asks before
writing. Then move the card to the Pi, connect the cable — the **middle**
micro-USB port on a Pi Zero, not `PWR IN` — and the tool waits for the board and
checks name resolution over the new link.

For the Pi to reach the internet, share your computer's connection over that USB
interface: Internet Sharing on macOS, the Sharing tab on Windows (which also
needs a one-time [RNDIS driver](https://github.com/raspberrypi/rpi-usb-gadget/releases)),
or *Shared to other computers* on the `usb0` connection on Linux. Then continue
above, connecting as `<your-user>@<your-hostname>.local`.

Full instructions, including per-platform troubleshooting, are in the
[install procedure](https://github.com/adrian-dybwad/Universal-Chess#no-wi-fi-set-the-board-up-over-the-usb-cable).

## Verify Download

```bash
sha256sum -c SHA256SUMS.txt
```

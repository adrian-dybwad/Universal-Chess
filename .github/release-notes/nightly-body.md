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

Download and install on your Raspberry Pi:

```bash
# Download the .deb file (run on the Pi)
wget https://github.com/__REPOSITORY__/releases/download/__TAG_NAME__/universal-chess___NIGHTLY_VERSION___all.deb

# Install -- apt resolves dependencies in one step (./ prefix required).
# --reinstall is needed because every nightly shares the version "__NIGHTLY_VERSION__".
sudo apt-get install -y --reinstall ./universal-chess___NIGHTLY_VERSION___all.deb
```

Or download the `.deb` file from the Assets section below and transfer it to your Pi.

When the install finishes, reboot the Pi and it should come up running
Universal Chess.

## Switch to Stable

To switch back to stable releases:
```bash
sudo apt-get install --reinstall universal-chess
```

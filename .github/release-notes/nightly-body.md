## Prepare a new SD card

Follow the [install procedure](https://github.com/adrian-dybwad/Universal-Chess#install-procedure)
in the README to prepare a new SD card before installing.

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

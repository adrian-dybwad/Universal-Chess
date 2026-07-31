## Prepare a new SD card

Follow the [install procedure](https://github.com/adrian-dybwad/Universal-Chess#install-procedure)
in the README to prepare a new SD card before installing.

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

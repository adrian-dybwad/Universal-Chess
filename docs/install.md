# Install procedure

Start clean on a new SD card. Do not reuse the original card from the Centaur --
keep it safe, you may want it later.

## Raspberry Pi (Raspberry Pi OS)

Image the card with the Raspberry Pi Imager.

1. In the Pi Imager, choose Raspberry Pi OS (64-bit) **Trixie**, with no apps or
   desktop (Lite). The 32-bit image also works, but the 64-bit image supports
   more engines.
2. In the Imager's advanced settings, configure the hostname, a user and password
   (any username works, you are not limited to `pi` -- just remember the
   password), Wi-Fi credentials, and enable SSH. Set any other options you like.
3. Write the image, insert the card into the Pi, and power it on. With Wi-Fi
   configured correctly the Pi usually joins your network within a minute or two.
   No screen or keyboard required.
4. SSH into the Pi and update the base system:
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```
5. Run the install command for your choice of nightly or release build (see the
   [Releases page](https://github.com/adrian-dybwad/Universal-Chess/releases) for
   the exact command and `.deb` for the target version).
6. Wait for the install to complete, reboot, and the board should come up running
   Universal Chess.

### No Wi-Fi? Set the board up over the USB cable

The procedure above assumes the Pi can join your Wi-Fi. If it cannot — no
network in reach, a password that turns out to be wrong, a 5 GHz-only access
point, or a plain Pi Zero with no radio at all — the board never appears, and
with no screen or keyboard there is nothing to log into to fix it.

So the fallback has to be in place **before** the first boot, put there from the
same machine that imaged the card. `enable_usb_gadget.py` makes the Pi appear as
a USB Ethernet adapter, so a single cable carries both the login and, once you
share the host's connection, the internet access the install needs.

The steps below replace the procedure above end to end — you never need Wi-Fi.

> **IMPORTANT: take the Pi out of the Centaur before you plug it into a
> computer.** In the Centaur the Pi is powered by the board, and it may not like
> being powered by the board and by USB at the same time. It may well be fine —
> but connect both at your own risk, as with all modding of this game. Do the
> whole procedure below with the Pi on your desk, and refit it at the end.

Download `enable_usb_gadget.py` from the
[Releases page](https://github.com/adrian-dybwad/Universal-Chess/releases). It is
one self-contained file and needs Python 3.9 or newer on your computer — nothing
else to install.

1. Image the card with the Pi Imager, choosing Raspberry Pi OS **Trixie** Lite.
   Use the **32-bit** image: that is the combination this route was tested on.
   The 64-bit image may work too, and supports more engines, but has not been
   tested over USB. In the Imager's advanced settings set the hostname, a user
   and password, and enable SSH. Wi-Fi credentials are optional here — the point
   of this route is that you do not need them.
2. When the Imager finishes it ejects the card, so your computer can no longer
   see it. **Take the card out of the reader and put it straight back in** to
   remount the boot partition. Do not boot it in the Pi yet; these changes have
   to be on the card before its first boot.
3. Run the tool on your computer — not on the Pi:

   ```bash
   python3 enable_usb_gadget.py --dry-run   # show what it would change
   python3 enable_usb_gadget.py             # make the changes
   ```

   On Windows use `py enable_usb_gadget.py`. If Python is missing, install it
   from [python.org](https://www.python.org/downloads/) or the Microsoft Store.

   The card is found automatically — `/Volumes/bootfs` on macOS,
   `/media/<user>/bootfs` on Linux, a drive letter on Windows. If detection
   fails, or more than one card is mounted, pass `--boot` with the path to the
   boot partition. The tool describes the card it found and asks you to confirm
   it before writing anything.

   Once the card is written the tool pauses and asks whether to wait for the
   board. Do steps 4 and 5 before answering it.

4. Turn on internet connection sharing, before you connect the Pi. Without it
   the Pi gets a login but no route out, and the `apt` steps below fail:

   | Host | Where to turn it on |
   | --- | --- |
   | **macOS** | System Settings > General > Sharing > Internet Sharing, sharing to the RNDIS/Ethernet Gadget |
   | **Windows** | Network Connections > right-click your internet adapter > Properties > Sharing tab > allow sharing to the gadget adapter |
   | **Linux** | NetworkManager: set the `usb0` connection's IPv4 method to *Shared to other computers* |

   Windows also needs a one-time RNDIS driver, from
   [rpi-usb-gadget releases](https://github.com/raspberrypi/rpi-usb-gadget/releases).
   macOS and Linux need no driver.

5. Eject the card, put it in the Pi, and connect the USB cable to your computer.
   On a Pi Zero use the **middle** micro-USB port, the one next to the
   mini-HDMI — the port marked `PWR IN` supplies power only and will not
   enumerate.

   Leave the tool running. It waits for the board to appear and then checks that
   name resolution works over the new link. A Pi Zero's first boot runs
   cloud-init on slow hardware; the tool waits 300 seconds before giving up,
   which `--wait-timeout SECONDS` extends.

   On macOS the tool watches for the `bridge` interface that Internet Sharing
   creates, which is why step 4 comes first — with sharing off it waits the full
   300 seconds and reports that the link never appeared. On Windows it cannot
   enumerate the gadget interface at all and will always report that, whether or
   not the board came up; pass `--no-wait` there and go straight to the next
   step once the Pi has had a minute or two to boot.

6. SSH into the Pi with the hostname you set in the Imager, and update the base
   system:

   ```bash
   ssh <your-user>@<your-hostname>.local
   sudo apt update && sudo apt upgrade -y
   ```

7. Run the Universal Chess install commands from the Installation section of the
   latest nightly on the
   [Releases page](https://github.com/adrian-dybwad/Universal-Chess/releases),
   then reboot.
8. Open the Pi's web interface in a browser on the same computer, at
   `http://<your-hostname>.local/`. This is a good moment to install any UCI
   engines you want, including the original Centaur engine.
9. Shut the Pi down cleanly with `sudo poweroff`, unplug the USB cable, and
   refit the board in the Centaur.

If something does not work, start here.

**Be patient on the first boot.** It takes several minutes for the board to
become reachable over the USB link, and longer on a plain Pi Zero. A device that
has not appeared yet, a link that flaps, a ping that fails and an SSH that hangs
are all normal in that window and do not mean the setup failed — cloud-init is
still doing first-boot work, and the interface is reconfigured as it goes. Give
it the full 300 seconds the tool waits before you start diagnosing, or you will
be chasing a fault in a board that was only still booting.

The Pi takes its address by DHCP from the host, so it has no fixed IP and the
address changes between boots. Prefer the hostname. If `.local` does not resolve,
find the address your host leased it:

| Host | Command |
| --- | --- |
| **macOS** | `arp -an \| grep bridge`, or read `/var/db/dhcpd_leases` |
| **Windows** | `arp -a` |
| **Linux** | `ip neigh show dev usb0` |

If the board answers by IP but names do not resolve, the usual cause is a
resolver on your computer that started before the USB interface existed and
never bound to it. The board says so at login, and the same tool diagnoses and
offers to fix it:

```bash
python3 enable_usb_gadget.py --check-dns --fix
```

One further consequence: enabling USB gadget mode **disables the Pi's USB host
port**, so no USB peripherals while it is on. Nothing in Universal Chess uses
that port.

Full documentation, including troubleshooting for each stage, is in
[`tools/sd-card-setup/README.md`](../tools/sd-card-setup/README.md).

## Orange Pi (Armbian)

Orange Pi boards are imaged with the Armbian imager instead of the Raspberry Pi
Imager, and the user from the imager profile is finished off on first login, so
the first SSH session is as `root`.

1. Get the Armbian imager: <https://imager.armbian.com/>
2. Select your board and **Armbian 26.11.0 Minimal**.
3. Configure a profile to set the Wi-Fi and user info. The user to create on
   first login can be anything you like.
4. Start the board with the newly flashed SD card. It should connect to the
   Wi-Fi network you specified. If it does not, flash again, making sure the
   profile with the right Wi-Fi credentials is selected.
5. Log in over SSH -- initially as user `root` -- to set up the new user you
   specified in the profile and to set the locale.
6. Restart with `sudo reboot now`, then log in as the newly created user.
7. Copy the install command from the latest nightly on the
   [Releases page](https://github.com/adrian-dybwad/Universal-Chess/releases)
   and run it.

# Changelog

All notable changes to Universal Chess will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - Unreleased

### Overview

Universal Chess 2.0 is a major rewrite of the DGTCentaurMods project, focusing on
code quality, maintainability, and extensibility. The codebase has been completely
reorganized with proper module structure, comprehensive tests, and modern CI/CD.

### Added

- **System Information shows the OS edition**: the card listed the kernel
  release but not whether the image was Raspberry Pi OS Lite or Desktop,
  or Armbian Server vs Desktop, so two boards with the same kernel looked
  identical. The collector reads `/etc/os-release` (pretty name and
  VARIANT), `/etc/rpi-issue` (the pi-gen Lite/Desktop/Full stage),
  installed desktop packages, and the systemd default target. Raspberry Pi
  OS 64-bit still identifies as Debian, so that pretty name is rewritten to
  Raspberry Pi OS with the version. A generic headless Debian is not
  labelled Lite. The Operating system row sits with Device and Kernel
  under Show details.

- **Original Centaur tab shows the imported app's display driver**: Direct
  Mode on a V1 PCB paints nothing because that original software speaks
  UC8151D on its own GPIO/SPI map, but Settings had no view of what the
  uploaded build actually uses. The tab now scans the imported Centaur
  tree (the `centaur` binary, bundled `spidev.so` / `RPi/_GPIO.so`, and
  any `.py` beside them) for panel class, controller family, SPI device
  or path template, GPIO numbering, and pin names or numbers. The scan
  runs when Show details is opened, so visiting the tab does not walk
  the uploaded binary. A different SD image updates the card; Universal
  Chess's own wiring and the translate shim are not used as stand-ins. Pin numbers come from
  readable ``EPAPER_RESET = 12`` assignments when a build still has
  them, or from ARM ``mov r0, #12`` next to a load of the pin name when
  Nuitka left that pairing. Official DGT Nuitka 0.6.5 interned the BCM
  integers as shared small ints with no static name pairing, so those
  uploads show the pin names and the "not stored as readable constants"
  note rather than invented 12/16/7/18. They are never filled in from
  Universal Chess's map.

- **Original Centaur tab shows GPIO/SPI used at runtime**: official DGT
  Nuitka builds do not store BCM pin numbers as readable constants, so
  the static scan above cannot list them. In Translate Mode the display
  shim now reports every GPIO line and ``/dev/spidev`` node that process
  actually opens; the gateway writes the set so the diagnostics card can
  show those numbers after Universal Chess restarts, with this build's pin
  names on the same line. A name is attached to a specific BCM pin only
  when that build stored the pairing; otherwise the names are listed
  beside the numbers rather than assigned from Universal Chess's map.
  Direct Mode does not load the shim and cannot record pins.

- **Display probe result is recorded in the Settings event log**: startup
  wrote the outcome only to the board log and `display_status.json`.
  Overlay missing, gpiochip/spidev permission errors, and other
  non-timeout init failures never appeared under Diagnostics, and a
  successful probe had no durable "which panel" line. Each boot now
  appends one `display` event: which controller initialized (UC8151D V2
  or SSD1680 V1), that no panel responded, or the init error that
  skipped the other driver.

- **Orange Pi boards are known to have onboard Wi-Fi and Bluetooth**: the
  model classifier only knew Raspberry Pi strings, so an Orange Pi fell through
  to the plain Pi Zero rule (which shares the word "zero") and was treated as
  having no radios at all. Both indicators reported the hardware absent and
  hid themselves, and the polling loop skipped the radios entirely. Orange Pi
  models are now classified as equipped, so the radios stay offered even if
  firmware fails to bind.

- **Orange Pi e-paper uses spi-gpio on the Centaur SPI1 header pins**:
  Hardware SPI overlays mux the wrong pads (SPI0 is onboard NOR, SPI1 is the
  Pi SPI0 header). With the 40-pin header soldered, live pinctrl on H618
  named the Centaur panel pins PI11/PC12/PH9/PI1 (gpiochip1 267/76/233/257)
  plus PI2/PI3/PI4 for MISO/SCLK/MOSI. The e-paper backend drives those
  lines through libgpiod, and postinst loads a spi-gpio user overlay on
  Orange Pi instead of `spidev0_0` / `spidev1_0`. The H616 overlay
  `compatible` list is H616/H618 (Zero 2W). H3/H5 40-pin boards (PC, One,
  Lite, Plus, Plus 2, Plus 2E, PC Plus, Prime, PC 2) load
  `uc-centaur-spi-gpio-h3`, which also enables UART3 on header 8/10
  (`/dev/ttyS3`); postinst adds `overlays=uart3` on those boards. Rockchip
  Orange Pi boards (4/5/3B/800) skip Allwinner `&pio` / `&ccu` phandles and
  do not load a spi-gpio overlay. SPI bus numbers stay un-hardcoded:
  userspace opens the master whose driver is spi-gpio.

- **Orange Pi chess UART follows the board profile**: The chess MCU link was
  hardcoded to `/dev/serial0`, a Raspberry Pi udev alias that Armbian does
  not create. SyncCentaur now opens the profile UART (`/dev/ttyS0` on H616
  Orange Pi, `/dev/ttyS3` on H3/H5 40-pin boards, `/dev/serial0` on a Pi)
  and refuses to guess when the profile has no UART: an unrecognized board
  fails with a named error rather than opening a device that is not the MCU.

- **Orange Pi USB Ethernet gadget uses the musb UDC, not dwc2**: The
  Connectivity USB gadget setting called `rpi-usb-gadget` and required a
  `dtoverlay=dwc2` line in `config.txt`. Armbian on Allwinner has neither; the
  H618 OTG controller is already `dr_mode=peripheral` with UDC
  `musb-hdrc.4.auto`, and `g_ether.ko` is in the kernel. Client now loads
  `g_ether` and persists it in `modules-load.d`, and writes a netplan DHCP
  stanza for `usb0` (stock Armbian only matches `e*`). Auto and Shared stay
  Pi-only: they need the vendor ICS switcher, NetworkManager, and dnsmasq,
  none of which this image has.

- **Orange Pi can run the 32-bit Centaur binary**: Raspberry Pi OS 64-bit
  kernels set `CONFIG_COMPAT=y`, so `libc6:armhf` is enough for the imported
  armhf `centaur` ELF. The Armbian sunxi64 kernel was measured with
  `# CONFIG_COMPAT is not set`, which leaves that binary as `Exec format
  error`. Centaur import now installs `qemu-user-static` (binfmt) on a host
  that cannot exec AArch32, and skips it on Pi OS 64-bit. Which host is which
  is decided by running the armhf loader `libc6:armhf` has just installed and
  looking for `ENOEXEC`, not by reading `CONFIG_COMPAT` out of
  `/proc/config.gz`: that file needs `CONFIG_IKCONFIG_PROC`, which Raspberry Pi
  OS does not enable, so a config-file check would answer "no COMPAT" on every
  Pi and pull qemu plus its binfmt handlers onto boards that never needed them.
  A host that still refuses AArch32 after the install now fails the import
  rather than reporting success on a board whose `centaur` cannot launch.
  Installing `qemu-user-static` is not itself enough on arm64. It registers
  its handlers through systemd-binfmt and ships a `/usr/lib/binfmt.d` entry
  for every architecture except `qemu-arm` and `qemu-aarch64`, the two the
  packaging treats as natively runnable. On the Orange Pi that left the
  package installed and every other architecture registered -- including
  big-endian `qemu-armeb`, which cannot run Centaur -- while `./centaur`
  still failed with `Exec format error`. Import now writes
  `/etc/binfmt.d/qemu-arm.conf` and reloads systemd-binfmt, so the handler
  is restored on every boot and survives a qemu upgrade. Raspberry Pi OS
  64-bit never reaches that path: the kernel already runs AArch32, so no
  handler is written even on a Pi that has the qemu package for other
  reasons, and native 32-bit execution is never routed through qemu. When
  that provisioning step fails, the import error now points at Settings >
  System and Check for OS updates (the control that refreshes apt indexes)
  instead of a generic network hint with no in-app next step.

- **Handing over to the original Centaur stops UC's own board polling**: once
  the binary could execute, every launch still bounced straight back to the
  menu. Centaur's startup handshake failed four times with `Initial PING:
  Command failed`, whereupon it powered itself off and exited 1. The handoff
  released the serial board, but nothing stopped the battery poller, which asks
  the controller for `DGT_SEND_BATTERY_INFO` every five seconds and simply
  reopens whatever now sits at the node. UC and Centaur were two masters on one
  board link, each consuming the replies the other was waiting for -- UC logging
  its own `Timeout for DGT_SEND_BATTERY_INFO` while Centaur's PING starved
  between them. Stopping the UC service by hand and running Centaur against the
  same UART produced no PING failure at all, which is what identified the
  poller. Polling is now stopped before either handoff mode launches Centaur,
  ahead of the best-effort factory-marker write so a marker failure cannot leave
  it running.

- **The translate-mode serial hold no longer deadlocks Centaur's handshake**:
  the hold keeps board bytes from reaching Centaur until the first translated
  frame is painted, guarding a race where a battery event reaches its T5D
  driver while the framebuffer is still None. It held the *first* chunk
  whatever it was, including the reply to Centaur's own startup handshake. On
  an emulated host that handshake is the first board traffic, so its reply sat
  in the hold, `doPing` failed four times, and Centaur powered itself off
  without ever painting the frame the hold was waiting for -- the paint could
  only happen after the handshake it was blocking. Widening the timeout cannot
  help and shortening it only trades one race for another; the hold now lifts
  as soon as Centaur transmits, since a reply to Centaur's own request is not
  the unsolicited chatter the gate exists for. Board chatter arriving before
  Centaur says anything is still held.

- **The display shim satisfies Centaur's SPI node on boards that lack it**:
  Centaur's panel driver opens `/dev/spidev1.0`, the second SPI controller,
  where the T5D sits on a Pi Zero. A board that drives its panel by bit-banging
  GPIO never creates that node, so the open returned ENOENT and Centaur died in
  `epaperT5D.__init__` with `FileNotFoundError` before drawing anything. The
  shim now substitutes for an absent `spidev` node exactly as it already does
  for a missing `/dev/gpiomem`, and reports success for the spidev config
  ioctls on it. Nothing is lost: in translate mode every SPI transfer is
  swallowed and forwarded to the gateway, so the node is a handle to hold open,
  not a bus to drive. Whether to substitute is decided per open by testing for
  the node, so a board whose panel really is on SPI keeps driving its own bus.

- **Leaving the original Centaur says so on the panel**: returning is not a quick
  swap. The restart settles for three seconds and Universal Chess needs roughly
  another fifteen to import and paint its startup splash, and across that whole
  gap the panel held Centaur's last frame with nothing to say the exit had
  registered -- on hardware that reads as the board having crashed and powered
  itself off, and was reported as exactly that. A "Returning..." splash is now
  painted the moment Centaur exits, before the restart is requested (afterwards
  would never render, since the restart kills the process that would draw it).
  Translate mode still owns the panel at that point, and e-ink holds an image
  with no power, so the splash stays up across the restart until the startup
  splash replaces it. Direct mode gave the panel away, so it takes the hardware
  back first through a new `Manager.reacquire_hardware()`, the counterpart of the
  release the handoff performs: it forces the next refresh to re-run the panel's
  `init()`, because the release settled the panel outside the scheduler and left
  its state saying the hardware was still open, so without that the refresh would
  write to a closed device. That re-acquire is best-effort, like the panel settle
  in the release -- an exception there would otherwise skip the restart and leave
  the unit stopped with a dead board, which is far worse than a missing message.
  The splash is not drawn when the Centaur binary is missing: nothing was handed
  over, nothing restarts, and the live menu must not be replaced by a message
  about a return that did not happen.

- **The way out of the original Centaur is now stated where it is needed**:
  holding BACK on the board exits Centaur, but nothing said so. The web page
  named only its own Return to Universal Chess button, so a user who closed the
  tab, or who walked up to the board without a phone, had no way to learn the
  gesture existed. The splash shown while Centaur starts now carries it, that
  being the last moment Universal Chess can address the panel before Centaur
  takes it over, and the web confirmation offers it alongside the button. Both
  say it only in translate mode. The gesture is implemented by the serial tap
  watching for a held BACK, and direct mode has no tap -- it hands the board port
  to Centaur outright -- so repeating the hint there would send a user to hold a
  button nothing is listening to, at a board that never answers, when the web tab
  was in fact their only way back. Direct mode therefore keeps the plain wording
  in both places. A test ties the button named in the message to the one the tap
  is configured to watch, so retargeting the gesture cannot silently leave the
  panel instructing users to hold a button that no longer exits.

- **Splash text now fits in every language**: the splash was laid out around
  English proportions and three translations had outgrown it. The French
  shutdown prompt "Appuyez sur [▶]" needs two lines, and the battery was drawn
  at a fixed offset below the message that assumed exactly one, so the charge
  reading had a line of text through it -- worse than no reading, because it
  stayed legible enough to be misread. The German byline wrapped to four lines
  in a band sized for three and the Dutch Centaur hint to six in a band holding
  five; word wrapping discards the lines it cannot place, so both simply lost
  their last line with nothing to indicate it. Behind all three, the message was
  built with a fixed height whatever was above or below it: a byline pushes it
  down 52px, and the status-bar variant is shorter still, so text was wrapped
  against room that did not exist and ran off the panel. The message is now
  given the space that actually remains, less what is drawn beneath it, and set
  with the same shrink-then-wrap fitting the game-over screen already used, so
  an over-long translation is set smaller rather than cut short. The battery
  follows the height the message really drew and is resolved at each read, so it
  still tracks a message replaced while the splash is up. English is unchanged,
  down to the battery sitting at the same pixel.

- **`measure_locale_fit.py` was measuring the wrong font**: the script that
  clears translated strings for the panel never registered a resource loader, so
  `get_font` fell back to PIL's default bitmap face -- which ignores the
  requested size and is about 10px against the 18pt asked for. Every string it
  has ever cleared was measured on a face roughly half the real one, which is
  how all three faults above passed review, and why the Dutch hint was reported
  as fitting on the night it was added. The loader is now registered before
  anything is measured, the band geometry is taken from the splash rather than
  restated (the copies had already drifted), and strings are measured through
  the widget's own fitted layout so the verdict is what the panel does,
  including reporting when a string was rescued by shrinking. A test renders
  every shipped locale with the bundled font and asserts the message stays clear
  of the battery, the battery stack stays on the panel, and no line of the
  byline or the hint is dropped -- the check that would have caught all three,
  and which fails without this fix.

- **Counted strings in the web app fell back to English**: the test holding the
  translation bundles to one key set compared them key for key, which for a
  counted string means demanding English's plural shape -- `_one` and `_other`
  and nothing else -- of every language. How many plural forms a counted string
  needs is a property of the language, not of the language it was written in:
  i18next asks CLDR for the category of the count and reads the key for that
  category, so any form English does not distinguish had no key to find and fell
  through to English. Spanish and French were mildly affected, both splitting off
  a category at exactly a million, which none of the seven counted strings here
  reaches. Polish was affected at every count: it distinguishes 1 from the 2-4
  band from 5-and-up, so a Polish page read "3 positions" and "12 packages can be
  upgraded" in the middle of Polish prose, and only a count of exactly 1 came out
  Polish. Nothing about the bundles looked wrong, because the bundles matched --
  which is why the parity test could not see it and reported them clean. The
  parity check now compares counted strings by their base key and asks
  `Intl.PluralRules` -- the same CLDR data i18next consults -- which forms each
  language owes, and a second test renders every counted string in every shipped
  language at a count from each of its categories and requires the result to come
  from that language's own bundle rather than any fallback. The Polish forms are
  spelt out in the test as well, since a check that selects the category the same
  way i18next does would still pass with `_few` and `_many` written into each
  other's slots.

- **The BlueZ self-heal runs only on a Raspberry Pi**: the workaround targets
  a BCM43430 firmware fault and was gated on nothing more than the presence of
  an hci device. Orange Pi carries a uwe5622, an unrelated part the workaround
  cannot help, so installing rebuilt BlueZ against a chipset that never had
  the bug. Boards other than a Raspberry Pi now skip it.

- **The package installs on Armbian instead of aborting**: the .deb
  hard-depended on `python3-rpi.gpio` (Pi BCM GPIO) and the postinst wrote
  `dtoverlay=spi1-1cs` into a `config.txt` that does not exist there, which
  under `set -e` left the package unpacked but unconfigured -- as did
  `usermod` exit 6 for Pi-only groups such as `kmem`. `Depends` now accepts
  `python3-libgpiod` as an alternative, Pi `config.txt` overlays are skipped
  when the file is absent, and absent groups no longer fail the configure.
  On Armbian or Orange Pi OS the postinst sets `console=none` /
  `extraargs=console=tty1` in `armbianEnv.txt` or `orangepiEnv.txt`, because
  this image's `boot.cmd` otherwise keeps ttyS0 on the kernel cmdline and a
  getty holds the chess UART.

- **Lichess Lobby on the web**: Settings → Players showed a credentials card
  (add/delete logins) under the name Lichess Settings, while the board lobby
  was Account, Ongoing Games, Challenges, and New Game. The Players card is
  now that same hierarchy, labelled Lichess Lobby on both surfaces. Account
  binds the Lichess slot (Accounts last, nested, for add/delete). Ongoing and
  Challenges list the live games for the bound login; selecting one, or New
  Game, starts it on the board through the same join the e-paper lobby uses.

- **Display > Text Size on more of the board UI**: The setting previously
  scaled only the coach statement and the analysis move list. It now also
  scales game-over and setup-status copy, chess-clock labels and names, the
  help dialog, info overlays, and icon menus. Large raises the menu's
  minimum row height so dense lists show fewer, taller buttons, and labels
  on a tall button wrap into the extra space instead of clipping. Medium
  remains the identity scale, so existing layouts are unchanged until the
  setting is moved. The Text Size picker still previews Small/Medium/Large
  at 13/16/20 rather than scaling those preview sizes a second time. Its help
  named only the coach text and the move list, which had been the whole of it,
  and now names everything the setting reaches.

- **Windows PowerShell troubleshooting on Original Centaur**: Importing from an
  SD card on Windows is blocked by two PowerShell errors that the download
  buttons did not mention: the shell refuses a bare `make-centaur-image.ps1` in
  the current folder, then refuses the `.\` form because the script is not
  digitally signed. Settings -> Original Centaur now has a collapsed
  Troubleshooting card with those two errors and the three remedies (one-run
  Bypass, Unblock-File for a downloaded script, and CurrentUser RemoteSigned),
  plus the Administrator requirement. The card stays on the tab after Centaur is
  installed, so a re-image does not bury the help inside Re-import.

- **Operating system updates in Settings**: The Software Updates card could
  install Universal Chess from GitHub, but Raspberry Pi OS packages (kernel,
  firmware, libraries) still needed an SSH session and
  `sudo apt update && sudo apt upgrade -y`. Settings -> System -> Software
  Updates now has an Operating system subsection: Check for OS updates counts
  upgradable packages, Update operating system runs the upgrade, and Reboot now
  appears when the OS says a reboot is required. Universal Chess itself stays
  on the GitHub updater above; the OS path holds that package only for the
  duration of `apt-get upgrade` so the two cannot fight. Privileged work goes
  through the pinned `uc-os-upgrade` helper (passwordless sudo grant, same
  pattern as the clock helpers) in a transient systemd unit so an upgrade that
  restarts the web service cannot kill apt. The board menu is unchanged.

- **USB Ethernet gadget mode in Connectivity settings**: Boards prepared with
  `enable_usb_gadget.py` (or an equivalent boot edit) had no in-app control for
  Off / Auto / Client / Shared, and nothing showed whether the live OS state
  matched the preference or still needed a reboot. Settings -> Connectivity now
  carries a USB Gadget control (after Bluetooth, before Chromecast -- the same
  catalog node on the board's Connectivity menu): on the web, the modes are
  radios with always-visible descriptions that name this board's own
  `http://<hostname>.local/` URL (and `http://10.12.194.1/` in Shared), warn that
  Shared can interrupt the host computer's normal Wi-Fi or Ethernet while the
  cable is connected (and that the board will share its own Wi-Fi internet with
  the computer when it has one), and note that Client needs Internet Sharing /
  ICS. Off mentions reaching the board over Wi-Fi or Ethernet only when the board
  actually has Wi-Fi (a plain Pi Zero without a dongle does not). A status
  readout under the radios reports desired vs live, whether boot still loads the
  gadget stack, whether they match, and when a reboot is required. Changing mode
  while connected over USB can drop the session; the web page asks for
  confirmation first. Privileged apply goes through the pinned
  `uc-usb-gadget-admin` helper (passwordless sudo grant, same pattern as the
  clock helpers). A board with no stored preference that is boot-prepared seeds
  desired to Client so the control does not read Off on a card the setup script
  prepared. When a reboot is still required for the preference to finish
  applying, the status readout offers a Reboot now button (same
  `/api/system/reboot` path as System -> Power).   Selecting Client or Shared pins
  the matching NetworkManager profile and disables the vendor ICS auto-switcher:
  stock `rpi-usb-gadget on` brings Shared up with Shared autoconnect, so without
  that pin a Client preference returned as Shared after reboot whenever the host
  was not offering ICS. Current packages also have no `shared` verb; Shared is
  applied the same pin path after `on -f`. Client/Shared apply also keeps the
  stock ``netplan-eth0`` profile off ``usb0``: that connection ships with
  ``match: {}`` (any ethernet), and on boards with no ``eth0`` (only ``usb0`` +
  Wi‑Fi) it claims the gadget as a DHCP *client*, fighting Shared (Pi as DHCP
  *server*) and leaving the host on a self-assigned address with an empty
  dnsmasq lease file.   With no ``eth0`` the profile is moved aside; with ``eth0``
  it is restricted to that name. Either way the original is kept beside it so Off
  can put the board back. ``netplan apply`` is avoided (it can hang and
  drop Wi‑Fi). Client/Shared also write early ``modules-load=dwc2,g_ether`` on
  the kernel cmdline (takes effect on the next reboot), so the gadget is armed
  before userspace starts and a host already plugged in at boot enumerates on
  its first try; the vendor tool's ``modules-load.d`` binds later than that.
  The gadget is armed once at boot and left alone from then on -- nothing
  reloads or rebinds the driver, and nothing needs to detect the cable, because
  an armed device enumerates whenever it is inserted. Client/Shared also need
  ``usb0`` under NetworkManager's control: NetworkManager's stock udev rule
  marks every gadget interface unmanaged, so the package ships a ``conf.d``
  drop-in claiming ``usb0``, without which the cable enumerates but the pinned
  profile never activates and the link never gets an address. Shared's fixed
  ``10.12.194.1`` no longer forces Link=Connected when the UDC is not attached.
  The Shared status readout reports the USB dnsmasq lease count so
  an idle server is visible. Shared-mode help now says that on macOS the
  per-device Internet Sharing switch for the USB gadget must be off as well
  as the master Sharing switch (leaving the gadget checked keeps
  ``bridge100`` / ``192.168.2.1`` and a self-assigned host address). Prepared detection accepts
  `/etc/modules-load.d/usb-gadget.conf` (what current `on` writes) in addition
  to cmdline `g_ether`, so a working gadget is not stuck offering Reboot forever.
  Web and board startup re-apply the stored preference when live disagrees, and
  the status readout polls every 10s (and on tab focus) while clearing stale rows
  if the board is unreachable during a reboot. After Off, ``rpi-usb-gadget``
  clears boot markers immediately but ``usb0``/``g_ether`` linger until reboot;
  the status no longer mis-labels that leftover as Client, and Reboot now stays
  offered until the netdev is gone. Turning Client/Shared back on after Off
  writes boot markers immediately while ``usb0`` is still absent; Reboot now is
  offered in that window too (vendor ``on`` itself says reboot to apply). An
  idle ``usb0`` with Client/Shared NM profiles still present (host not attached
  yet) reports Live Client rather than Off, so Match is not a false failure
  while waiting for the cable or Internet Sharing. The status readout also
  shows Host link (UDC attached / not attached / none) so a Matching Client
  with no cable is visible without looking like a mode failure. On the board
  e-paper USB Gadget select, the selected radio shows Connected or Disconnected
  (and the usb0 IPv4 when present). An address on usb0 always counts as
  Connected -- never Disconnected or ``No host`` beside an IP the session is
  using. The web status readout uses the same Connected/Disconnected wording
  and shows the Address (usb0 IPv4). Online
  account management moved
  from Connectivity to Players so credentials sit next to the account picker
  that uses them.

- **Auto USB gadget mode**: Client and Shared each require the user to know what
  the host computer is doing -- Client needs Internet Sharing on, Shared needs it
  off -- and the wrong choice looks like a broken cable. The USB Gadget control
  now offers Auto as well, which hands the link back to Raspberry Pi's
  `rpi-usb-gadget-ics.service`: it takes Client while the host offers a network
  over the cable and Shared when it does not, changing over as that changes. The
  board is reachable by name in either case, and at `http://10.12.194.1/` while
  it is in Shared. Auto is not the default and its description says why: the mode
  can change on its own, and each switch to Shared can interrupt the host
  computer's normal Wi-Fi or Ethernet. Applying Auto enables that unit and
  restores the autoconnect a fresh `rpi-usb-gadget on -f` leaves, undoing both
  halves of a Client/Shared apply -- enabling the unit while leaving a profile
  pinned against it is neither mode. Auto moves no connection itself, so
  selecting it cannot drop the USB session the user is most likely browsing over.
  Because `usb0` can only ever report a concrete mode, Auto's status reads as the
  mode the switcher currently holds plus whether that switcher is enabled, and
  Auto counts as matching for either Client or Shared. A board whose switcher is
  disabled is pinned rather than switching, so Auto reports Match No there and
  startup re-applies it; conversely a Client or Shared preference found with the
  switcher still enabled reports Match No, since the unit can move it at any
  moment. A switcher state that cannot be read stays Unknown and never
  contradicts an otherwise healthy mode. A boot-prepared board with no stored
  preference and the switcher still enabled -- what `enable_usb_gadget.py --auto`
  leaves -- now seeds Auto instead of Client, so the control matches the card.

- Applying a USB gadget mode no longer risks the board's ability to boot, and Off
  now reverses everything the other modes changed. The kernel command line is the
  one file in this feature whose corruption is unrecoverable without a card
  reader: a truncated `cmdline.txt` has no `root=` and does not boot, and this
  board is normally turned off by cutting its power. That edit is now written to a
  temp file and renamed over the original, so the old contents stay readable until
  the new ones are complete on disk; it is read back and checked afterwards, and
  restored from memory if what landed is not a single line naming a root device.
  A command line that is already blank, multi-line, or missing `root=` is refused
  rather than edited, so a file broken by something else is not overwritten with a
  second broken generation. It also extends an existing `modules-load=` parameter
  in place instead of appending a second one, matching what
  `tools/sd-card-setup` writes -- which occurrence of a repeated parameter wins is
  up to whoever reads it, and a board should not depend on that. Off now removes
  those two modules from the command line, keeping any others the parameter lists,
  and restores the `netplan-eth0` profile the on-modes moved aside: a mode setting
  that keeps boot-time edits after the user turns the feature off is one they
  cannot actually turn off. The Shared lease count is read through the same
  privileged helper as the mode changes instead of making NetworkManager's state
  directory world-traversable, which had exposed that directory to every local
  user to save one privileged read; boards that took that widening have it undone
  on the next mode change. The file edits moved out of the shell helper into
  `uc-usb-gadget-files.py` beside it, where they are linted and tested directly,
  including the failure paths (a full or read-only `/boot`, a write that verifies
  wrong) which previously could not be reached from a test at all.

- The USB Gadget help now says which socket the cable goes into: the Raspberry Pi
  Zero's own USB data port, inside the chess board, not the Centaur's charging
  port. The charging port is the only socket an owner can see, and it carries no
  data, so a correctly configured board looked broken -- the mode applied, the
  status card said Disconnected, and nothing on either surface said the cable was
  in the wrong place. Every mode in the control depends on that cable, so the
  requirement sits at the top of the widget, where it is read before the choice
  is made. The help now also says which end of the cable to reconnect. Measured
  on a Centaur board, unplugging and plugging back in restores the link at the
  Pi's own micro-USB socket and at a USB-A joint, but not at the host computer's
  USB-C port, which can leave that computer seeing no device at all. Nothing on
  the board can repair that -- the gadget is armed once at boot and never rebound,
  since rebinding a live controller wedges it until the power is cut, and only the
  host can begin enumeration -- so the help names the end that works.

- The USB gadget now introduces itself to the host computer as "Universal Chess
  USB Gadget". The product string in the gadget's USB descriptor is the only name
  a user ever sees for this connection -- macOS shows it as the hardware port in
  Network settings and as the entry in the Internet Sharing list, which is the
  list Shared mode's own instructions send them to -- and the Pi kernel compiles
  in "Raspberry Pi USB Gadget", which names the board rather than the product and
  matches nothing the app says. The package and `enable_usb_gadget.py` both write
  a `modprobe.d` drop-in setting `g_ether`'s `iProduct`; the module is loaded from
  userspace, so modprobe reads it. Only the string changes: the USB vendor and
  product IDs belong to Raspberry Pi and are left alone, since claiming an ID we
  do not hold would misidentify the device to every host. The name takes effect at
  the next boot -- nothing reloads `g_ether` to apply it sooner, because unloading
  it with a host attached wedges the controller. On a host that has already seen
  the board, the renamed device appears as a new interface, so an existing
  Internet Sharing selection has to be made once more against the new name.

- Help text on the board is now paged instead of being cut off. The HELP dialog
  drew wrapped lines from the top of the panel with no limit, so a tip longer
  than the thirteen lines it holds ran over the "Press any button" line and then
  off the bottom of the screen, where it was clipped without a mark -- the reader
  saw a tip that stopped mid-sentence and nothing said there was more. The USB
  Gadget mode descriptions are the texts that exposed it: Shared is 25 wrapped
  lines and Auto 23. Tips are now split into pages that fit the panel, with a
  "Page N of X" footer; UP and DOWN turn the page and any other button closes,
  the same keys and the same wrap-around used by the menu, the keyboard layouts
  and the analysis pages. The idle timeout that returns an unattended board to
  the menu now restarts on each page turn, since measured from when the dialog
  opened it would close mid-read on the second page. A tip that fits on one page
  is unchanged: no footer, and any button closes it. The wrap, the page split,
  the page cursor and the footer are one widget shared with the coach statement
  panel, which pages the same way on OK -- previously each panel had its own
  copy of that logic, and only one of them had it at all.

- Long-press OK on a highlighted move in the live move list offers taking the
  game back to that position, or starting a new recorded game from it. Short OK
  is unchanged (pages a coach statement, or forces a full refresh). Holding OK
  for a second while a played move is selected opens Take back to this position
  (undo every later move; the pieces are then guided back) and New game from
  this position (copies the moves through that ply into a fresh recorded game;
  the current game stays in history to resume later). Take back is unavailable
  when the highlighted move is already the last one, or when the opponent cannot
  take back (Lichess). A long-press OK anywhere else still cancels the press the
  way the other keys do.

- Shared mode's instructions now quote the gadget's name rather than describing
  it, so the switch to turn off in macOS's Internet Sharing list can be found by
  reading them.

- On the web, the USB Gadget card no longer prints its own title twice. The card
  heading and the control inside it both took the catalog node's label, so
  "USB Gadget" appeared on consecutive lines; the control is now labelled Gadget
  Mode, which also gives the radio group an accessible name that describes what
  it sets rather than repeating the heading. The board's menu is unaffected --
  it has never shown both.

- **Turkish UI**: Türkçe is now offered in Settings > Language and translates the
  board menus, e-paper widgets, and the web app. Like Dutch, Polish and Italian,
  Turkish was not already on the list, so adding it meant registering the locale
  itself -- the supported set, the selector label, and the plain-English name the
  AI coach is instructed with -- as well as writing all three bundles: the 338
  board strings, the menu-catalog overlay, and the 983-key web bundle. It sits
  after Italian at the end of the selector, on the same basis: it is an original
  DGT Centaur firmware language that is not in the ten most-spoken the list
  opens with. With Turkish, every language the original Centaur firmware offered
  now has a full Universal Chess UI.
  - The copy is informal (sen, not siz), matching Lichess's own Turkish interface
    and Turkish consumer software, and uses the chess vocabulary a player
    expects: vezir for the queen, fil for the bishop, at for the knight, kale,
    piyon, şah, mat, pat and beraberlik.
  - Colour labels are nominative Beyaz/Siyah, so the Lichess start message and
    the king-lift resign prompt name the side rather than governing the colour.
    Counted seconds on the board use the `s` symbol, because the board's own
    `t()` has no plural mechanism. The web counted strings use `_one` and
    `_other`, which is the pair CLDR assigns Turkish.
  - RESUME on the Play tile reads DEVAM. The tile's help quotes the word the tile
    shows. Rated Lichess games are labelled Reytingli.
  - Lengths were measured through the widgets that draw them:
    `scripts/measure_locale_fit.py` reported four board strings that fit in
    English and not in Turkish. All four stay at 17pt, because the expected words
    (doğrulanamadı, durdurulamadı, desteklenmiyor) are a single unbreakable stem
    wider than the column at 18pt, and shrinking loses nothing. The bundled font
    draws ı and İ. The splash tagline is the proverb the English byline inverts:
    "Hediyenin atına bakılmaz".
  - The exemption list recording which catalog strings need no translation was
    re-read against Turkish: `min` stays exempt, Chicago, Denver, Auckland and
    UTC stay exempt because they are spelt the same, while the cities Turkish
    renames (London becomes Londra, Moscow Moskova, Kolkata Kalküta, Shanghai
    Şanghay, Sydney Sidney) are translated like any other string.

- **Russian UI**: Русский was already offered in Settings > Language -- it sits
  in the ten most-spoken the selector opens with, so the AI coach could write
  Russian -- but the board menus, e-paper widgets, and the web app stayed English.
  The three bundles now ship: the 338 board strings, the menu-catalog overlay,
  and the 983-key web bundle. The selector itself is unchanged.
  - The copy is informal (ты, not Вы), matching Lichess's own Russian interface
    and Russian consumer software, and uses the chess vocabulary a player
    expects: ферзь for the queen (not королева), слон, конь, ладья, пешка, шах,
    мат, пат and ничья.
  - Russian inflects in four CLDR bands, the same count Polish needs. The seven
    counted web strings now carry `_one`, `_few`, `_many` and `_other`, so
    "1 позиция", "3 позиции" and "5 позиций" each read correctly, a 21 takes the
    singular again, and a fraction takes the genitive singular. Colour labels are
    nominative Белые/Чёрные, so the Lichess start message and the king-lift
    resign prompt name the side ("Твой цвет: Белые") rather than governing the
    colour. Counted seconds on the board use the `s` symbol, because the board's
    own `t()` has no plural mechanism.
  - RESUME on the Play tile reads ДАЛЕЕ, the short word that fits the 32pt slot
    the way German WEITER does; ПРОДОЛЖИТЬ would have to shrink. The tile's help
    quotes the word the tile shows. Rated Lichess games are labelled Рейтинговая.
  - Lengths were measured through the widgets that draw them:
    `scripts/measure_locale_fit.py` reported sixteen board strings that fit in
    English and not in Russian. Eleven were reworded to fit at full size
    (Соединение, Запускаю, Продолжаю, Чиню рассылку, Неверный токен API, and
    similar). Five stay at 17pt because the word a Russian reader expects does
    not fit at 18pt: `lichess.seek.casual` keeps товарищеская, `setup.mode_title`
    keeps РЕЖИМ РАССТАНОВКИ, the two promotion-position titles keep превращение,
    and opposite-bishop draws keep разнопольные слоны. The bundled font draws
    Cyrillic, and the splash tagline is the proverb the English byline inverts:
    "Дарёному коню в зубы не смотрят".
  - The exemption list recording which catalog strings need no translation was
    re-read against Russian: `min` and UTC stay exempt, while every city Russian
    respells in Cyrillic (London Лондон, Chicago Чикаго, São Paulo Сан-Паулу,
    Auckland Окленд) is translated in the overlay.

- **Italian UI**: Italiano is now offered in Settings > Language and translates the
  board menus, e-paper widgets, and the web app. Like Dutch and Polish, Italian
  was not already on the list, so adding it meant registering the locale itself
  -- the supported set, the selector label, and the plain-English name the AI
  coach is instructed with -- as well as writing all three bundles: the 338 board
  strings, the menu-catalog overlay, and the 983-key web bundle. It sits after
  Polish at the end of the selector, on the same basis: it is an original DGT
  Centaur firmware language that is not in the ten most-spoken the list opens
  with.
  - The copy is informal (tu, not Lei), matching Lichess's own Italian interface
    and Italian consumer software, and uses the chess vocabulary a player
    expects: donna for the queen, alfiere for the bishop, cavallo for the knight,
    pedone, scacco, scacco matto, stallo and patta.
  - Italian, like Spanish and French, distinguishes a CLDR `many` form at exactly
    a million. The seven counted web strings carry that form as well as `_one`
    and `_other`, so a count of 1 000 000 stays Italian instead of falling
    through to English.
  - Colour labels are nominative Bianchi/Neri, so the Lichess start message and
    the king-lift resign prompt name the side ("Colore: Bianchi") rather than
    governing the colour. Counted seconds on the board use the `s` symbol,
    because the board's own `t()` has no plural mechanism.
  - RESUME on the Play tile reads CONTINUA. The tile's help quotes the word the
    tile shows. Rated Lichess games are labelled Valida.
  - Lengths were measured through the widgets that draw them:
    `scripts/measure_locale_fit.py` reported ten board strings that fit in
    English and not in Italian. Eight were reworded to fit at full size (informal
    verbs such as Attendo and Disconnetto, Scollegamento fallito, Nuova versione,
    Installazione in corso). Two stay slightly smaller: `update.checking` keeps
    "Cerco aggiornamenti..." at 16pt because that is the word an Italian reader
    expects for looking up updates, and `setup.mode_title` keeps PREPARAZIONE at
    17pt. The bundled font draws Italian, and the splash tagline is the proverb
    the English byline inverts: "A caval donato non si guarda in bocca".
  - The exemption list recording which catalog strings need no translation was
    re-read against Italian: `min` stays exempt, and Chicago, Denver, Auckland
    and UTC stay exempt because they are spelt the same, while the cities Italian
    renames (London becomes Londra, Paris Parigi, Moscow Mosca, Kolkata Calcutta,
    São Paulo San Paolo) are translated like any other string.

- **Polish UI**: Polski is now offered in Settings > Language and translates the
  board menus, e-paper widgets, and the web app. Like Dutch, Polish was not
  already on the list, so adding it meant registering the locale itself -- the
  supported set, the selector label, and the plain-English name the AI coach is
  instructed with -- as well as writing all three bundles: the 338 board strings,
  the menu-catalog overlay, and the 981-key web bundle. It sits after Dutch at
  the end of the selector, on the same basis: neither language is in the ten
  most-spoken the list opens with.
  - The copy is informal (ty, not Pan/Pani), matching Lichess's own Polish
    interface and Polish consumer software, and uses the chess vocabulary a
    Polish player expects: hetman for the queen, goniec for the bishop, skoczek
    for the knight, bierki for the pieces, remis, pat and szach mat.
  - Polish inflects where none of the languages before it did, and two board
    strings substitute a colour label that is fixed in the nominative ("Białe"),
    which no Polish verb governs. Rather than ship broken grammar, the Lichess
    start message and the king-lift resign prompt label the side instead of
    governing it ("Twój kolor: Białe"). Counted seconds on the board use the `s`
    symbol, because the board's own `t()` is a flat key lookup with no plural
    mechanism at all, so there is nowhere to put the three endings Polish needs
    for 1, for 2-4 and for 5 or more.
  - The web app does have a plural mechanism, and Polish is the first shipped
    language to need more of it than English: four forms where English has two.
    The seven counted strings now carry all four, so "1 pozycja", "3 pozycje"
    and "5 pozycji" each read correctly, and a fraction takes the genitive
    singular the same way a Polish reader writes "1,5 pozycji". Package counts
    were reordered to "Można zaktualizować {{count}} pakiety", which is where a
    Polish sentence puts the verb.
  - RESUME on the Play tile reads WZNÓW. It sets at 29pt in the tile's 32pt slot,
    which is what English RESUME already does at 30pt, so the accurate word was
    kept rather than traded for a shorter, vaguer one; KONTYNUUJ would have been
    shrunk to 20pt, much as FORTSETZEN is to 19pt. The tile's help quotes the
    word the tile shows.
  - Lengths were measured rather than judged by eye, through the widgets that
    draw them: `scripts/measure_locale_fit.py` reported five board strings that
    fit in English and not in Polish, four of which were changed to fit at full
    size and read at least as well. The fifth, `splash.starting`, keeps
    "Uruchamianie..." at 16pt, because the alternative that fits at 18pt is not
    the word a Polish reader expects for a system starting, and shrinking loses
    nothing. The bundled font draws all eighteen characters Polish adds, and the
    „...” quotes its typography uses.
  - The exemption list recording which catalog strings need no translation was
    re-read against Polish rather than inherited: `min` stays exempt because it
    is the SI symbol for minute in Polish too, and the four place names spelt
    identically stay exempt, while the cities Polish renames (Moscow becomes
    Moskwa, London Londyn, Kolkata Kalkuta) are translated like any other string.

- **Dutch UI**: Nederlands is now offered in Settings > Language and translates
  the board menus, e-paper widgets, and the web app. Unlike the four languages
  before it, Dutch was not already on the list: the selector held the ten
  most-spoken languages, so adding it meant registering the locale itself --
  the supported set, the selector label, and the plain-English name the AI coach
  is instructed with -- as well as writing the three bundles. It sits after the
  other ten because it is there on a different basis: the hardware is a DGT
  board, made in the Netherlands, so its home market reads Dutch.
  - The copy is informal (je, not u), matching how Dutch consumer products and
    Lichess's own Dutch interface address the reader.
  - Lengths were measured rather than judged by eye: every string was rendered
    through the widget that draws it, at the panel's real 120-pixel column, and
    compared against the English it replaces. `scripts/measure_locale_fit.py`
    keeps that check available for the next language. The board's font already
    draws every character Dutch adds.
  - RESUME on the Play tile reads VERDER rather than the literal HERVATTEN,
    which measured too wide for the tile at full size and would have been shrunk
    to fit; the tile's help quotes the word the tile actually shows.

- **German UI**: Selecting Deutsch now translates the board menus, e-paper
  widgets, and the web app. Deutsch was already in the language selector and the
  coach already wrote in German, but no UI bundle existed, so the board itself
  stayed English. All three bundles now ship a complete German set -- the board
  strings, the menu-catalog overlay, and the web app -- and the SPA treats `de`
  as a supported locale so it follows the device.
  - German runs about a third longer than English, and the panel is 128 pixels
    wide, so the copy was measured through the widgets that draw it rather than
    written to length by eye. One string overflowed and was reworded; the rest
    fit, including the game-over headline at every Display > Text Size.
  - The German uses the chess vocabulary a German player expects (Partie, Zug,
    Remis, Schachmatt, Dame and Springer) and Lichess's own German wording for
    the lobby (Gewertet, Herausforderung, Konto), so the board reads like the
    site it connects to.
  - The exemption list that records which catalog strings need no translation
    was re-read against German rather than inherited: the time controls stay
    exempt because `min` is the SI symbol for minute in German too, while the
    time zones German renames (Moscow becomes Moskau) are translated like any
    other string.

- **French UI**: Selecting Français now translates the board menus, e-paper
  widgets, and the web app, not only the AI coach's remarks. English and Spanish
  already shipped translation bundles; French was listed in the language selector
  (the coach can write in any listed language from a name) but had no UI overlay,
  so the menus, splash, game-over screen, and web chrome stayed English. The
  board string bundle, the menu-catalog overlay, and the web i18n bundle now
  ship a complete French set, and the web app treats `fr` as a supported locale
  so the SPA follows the device instead of falling back to English.

- **Stoppable, resumable engine installs**: A source build can run for an hour,
  and until now the only way out was to let it finish or reboot the board, which
  threw the work away. An install can now be stopped and picked up later.
  - Stop from the Settings page, or from the options menu the board's progress
    screen opens with TICK. The build stops at the next command boundary; its
    process group is terminated so no compiler is left running.
  - BACK on the board's progress screen leaves the install running rather than
    stopping it, so the board can be used for something else while an engine
    builds. A status-bar indicator shows the build is still going, and the
    engine's own screen offers the way back to it.
  - Stopping keeps the build tree, so resuming continues from the objects already
    compiled instead of starting the build over. A resume point records the git
    ref that was building, and the tree is only reused when the ref still
    matches, so resuming cannot silently produce a binary from a different
    version.
  - Several engines can be paused at once, and starting a different engine's
    install no longer deletes them: each resume point is a marker file inside its
    own engine's build directory rather than a single global slot. An install
    interrupted by a restart or crash is recorded the same way at startup.
  - Starting an install retires that engine's own resume point, since an engine
    that is building is not paused. A card otherwise showed "Stopped at N%" and a
    dead Resume button beside the live progress bar for the whole rebuild, and a
    fresh (non-resume) install left behind a record of a tree it had re-cloned at
    a different ref. Every other engine's paused state is untouched.
  - Discard removes a paused install's tree and resume point to free the space.
    It is destructive and cannot be undone, so it asks for confirmation on both
    the web page and the board. The board offers it at the moment an install
    stops, which is when the user knows whether they want the work back, with
    "keep" as the focused option.
  - Install, stop, resume and discard now do the same thing from either surface,
    because there is only one of each. Engine installs run in the web process
    alone -- it already owned the persisted install state, the resume points, and
    the catalog, repair and custom-engine flows -- and the board asks it to act
    over the sockets that already connect the two, then renders the shared state.
    Previously each process installed on its own manager, so a build stopped on
    the board left a tree the web could neither resume nor reclaim, and both could
    start an install at the same time. The board's own install path is gone rather
    than bridged.
  - New endpoints `POST /api/engines/{stop,resume,discard}`; resume and discard
    name their engine in the body and require authentication, as does stop. Each
    is a single function shared by the HTTP route and the board's request, so the
    rules they enforce are not implemented twice.
  - Engine management on the board requires the web service to be running. It
    says so plainly when it is not, rather than appearing to work.

- **Reckless engine**: Added the Reckless UCI engine (Rust, embedded NNUE,
  AGPL-3.0) to the installable engine catalog. It is the project's first Rust
  engine; because Debian's apt Rust (1.63) is too old for its edition-2024
  sources, both the on-device and CI builds bootstrap a pinned rustup toolchain
  and build `no-syzygy` (no clang dependency). Prebuilt binaries are produced for
  arm64 and armhf.

- **Three-color (red/white/black) e-paper mode**: Opt-in `[display] three_color`
  switch for tri-color BWR panels, implemented in both the UC8151D (V2) and
  SSD1680 (V1) drivers (tri-color is a property of the panel, not the controller)
  - Fixes the black-into-red bleed by routing the B/W plane to the controller's
    B/W RAM and a parallel red plane to its red RAM (UC8151D `0x10`/`0x13`,
    SSD1680 `0x24`/`0x26`); the mono partial path had written B/W into the red RAM
  - Hybrid refresh: fast black/white updates during normal play; the slow full
    tri-color refresh (~12–15 s) only when red appears, changes, or clears
  - Highlights the checked king and checker, a threatened queen and its attacker,
    the game result line, and losing-side evaluation-graph bars in red
  - Live toggle in the Display tuning settings card and a red web-mirror preview
    (no reboot required)

- **Engine Registry**: Centralized management of UCI chess engine instances
  - Prevents duplicate engine processes
  - Automatic lifecycle management
  - Shared engine access across features

- **Engine Install Queue**: Background installation system for chess engines
  - Queue multiple engines for installation
  - Progress tracking with UI feedback
  - Cancel/clear queue operations
  - Install history

- **Update Checker**: Pull-based update system
  - Checks GitHub releases for new versions
  - Supports stable and nightly channels
  - Download and install from the device

- **Maia Engine Support**: Human-like neural network chess engine
  - Specialized ARM build script with memory management
  - Downloads all 9 ELO-rated weight files (1100-1900)
  - Single-threaded compilation for Raspberry Pi

- **Modern CI/CD**: GitHub Actions workflows
  - Automated testing on Python 3.9, 3.11, 3.13
  - Automated package builds on release tags
  - Nightly builds from main branch
  - Automatic release creation

- **Version Management**
  - VERSION file created during package build
  - `scripts/bump-version.sh` for semantic versioning
  - Proper version comparison for updates

- **Device clock control**: The board keeps no time of its own. It is an
  RTC-less Pi, so at every power-on its clock restarts from whenever it was last
  written and only the network corrects it -- and a board reached over the USB
  link to a laptop has no time source at all. Nothing showed this, so a board
  minutes or hours out was indistinguishable from a correct one until the wrong
  time surfaced somewhere else. Settings now shows what the board's clock reads,
  how far that is from the browser's, and whether network time is switched on
  and actually reaching a server -- "on" and "synchronised" are reported apart,
  because a board that cannot reach a time server reports the first without the
  second.
  - Network Time can be turned on or off from the web Settings page and from the
    board's own System menu, from one shared definition, so both surfaces show
    the same control in the same place.
  - With it off, "Set from this browser" writes the browser's current time to the
    board. This is offered only when sync is known to be off: `timedatectl`
    refuses to step a clock it is synchronising, and a state that could not be
    read is not evidence that it is not. Should sync be switched on between the
    reading and the click, the board's refusal is reported as such rather than as
    a generic failure.
  - A time to be written must fall between 2024-01-01 and 2100-01-01. A clock set
    far outside that range invalidates TLS certificates and reorders the game
    log; the bound is enforced by the privileged helper and again before it is
    called.
  - Both operations are root-only. They run through a single pinned helper
    granted passwordless sudo, which accepts two verbs and validates the argument
    of each, so the grant cannot be used to run anything else. A board installed
    by hand without the grant is told the change did not take, rather than
    failing silently.

- **Your Queen warning can be switched off**: The board warns the player on move
  that their own queen is attacked -- YOUR QUEEN on the display, an LED flash from
  the attacker to the queen, the queen and its attackers in red on a three-colour
  screen, and a banner on the web board. That is help some players do not want,
  and there was no way to decline it. Settings -> Game -> In-Game Alerts now
  carries a "Your Queen Warning" checkbox, on by default, on both the web page and
  the board's own Game menu.
  - Turning it off silences all four surfaces at once. Each of them used to
    re-derive the rule from the position itself, so a partial change would have
    left the LEDs flashing at a red queen with nothing on screen saying why. The
    rule now lives in one pure module (`state/alerts.py`) that every surface
    resolves through, with the preference as an argument.
  - Check has deliberately no such setting. An unanswered check makes every other
    move illegal, so hiding it would let the player build a position the board
    cannot accept, which is a different thing from withholding advice.
  - The change applies to a game in progress, from either surface, without a
    restart: a warning already on screen comes down at the next display refresh.

- **Move times in exported games**: An exported game carried the moves and
  nothing about the time they took, so a game reviewed later gave no sign of
  where the player burned their clock. Each move now carries the time it took,
  timed games also carry the mover's remaining time, and the game carries a
  standard time-control tag. The 1994 PGN standard has no per-move timing; the
  Proposed Supplement adds it by embedding commands in ordinary comments, which
  is what every other producer of move times emits, so the file stays readable
  by anything that reads PGN.
  - Times are reported to a tenth of a second. They are measured to the
    millisecond, and rounding to whole seconds discarded up to half a second
    from every move -- an error that accumulates the moment anything sums the
    moves, and one that collapsed a 4.4 second reply and a 4.6 second one onto
    figures with nothing to tell them apart. Remaining time stays at whole
    seconds, because the clock holds no finer reading to report.
  - Timing is taken from a monotonic timer at the instant a move is confirmed,
    not from the database row's timestamp and not from the wall clock. The row is
    written by a background worker an unbounded time later, so differencing those
    would charge the player for the queue; and the board's wall clock is stepped
    by network time shortly after boot, by an amount that lands squarely in the
    range of a real think time, so a wall-clock difference would report the
    correction as deliberation.
  - A takeback drops the retracted move's time and re-anchors, so a player who
    thinks for a minute, takes the move back and replays instantly is not charged
    the whole minute against the replayed move.
  - An engine's move is timed from when the engine starts thinking to when the
    player finishes transcribing it onto the physical board, because the player
    is occupied for that whole span and consecutive times have to sum to the
    length of the game. One consequence is worth knowing: an engine move's
    elapsed time does not reconcile against the remaining-time deltas, which stop
    the engine's clock when it displays its move.
  - An untimed game gets elapsed times only. Its clock reads zero for both sides,
    so reporting remaining time there would export a casual game as though both
    players had flagged on every move.
  - A time-odds control has no representation in the standard tag, which is read
    as applying to both players. The standard tag is written as "unknown" and the
    two sides are reported separately, so nothing false is claimed and nothing is
    lost. Writing the odds into the standard tag would either be rejected by a
    conforming parser or understate one side's budget.

- **Lichess is one play path**: Human vs Lichess now starts the same way as
  Human vs Engine (PLAY, or Lichess Lobby → Seek New Game). A new seek asks for
  the side the Players color control did not give the human -- White stays on
  player 1's physical side, Black on player 2, and the e-paper rotates rather
  than the pieces when the match assigns a color other than the one chosen.
  The seek offers the Game time control (whole minutes plus
  Fischer increment), rated from the Rated toggle, and the rating range from the
  bound account on the active host. Once the Board API stream starts, the
  e-paper clock follows Lichess remaining time and increment (untimed for
  unlimited correspondence). A second launcher had forced Human White and
  minutes+0, skipped PLAY's game widgets, and froze the board when its imports
  were stale. That launcher is gone. Ongoing and Challenges still join by id, then
  sit in the same game. A Lichess player chooses a credential listed as
  server:user (`lichess.org:Alice`, `lichess.dev:Bob`). Org and .dev are hosts
  on the Lichess plugin, not a second account type and not a Game toggle;
  the bound credential's host is the server the token is sent to. Credentials
  are managed under Players → Lichess Settings. The
  waiting splash
  lists the seek (clock, rated, color, host:user, rating range); when the stream accepts, the board remaps the human to
  the stream's color (White remains player 1, Black player 2), rotates the
  e-paper if they play Black, and shows "Game started / You play
  White" (or Black) until the first move or five seconds. BACK during seek cancels;
  Abort is on the in-game BACK menu while abort is still legal; Resign still ends
  on the game-over screen. Credentials are managed under Players → Lichess
  Settings → Accounts, not a Token row on Play. A leftover `[lichess]` token is
  promoted to a host:user credential on boot when the username is already
  cached (no network). `game.lichess_use_dev` selects `dev:` for that one
  copy, matching how player bindings already migrate. A token with no cached
  username stays in `[lichess]` until Lichess Settings can resolve it.

- **Ready-to-install update copy names the version**: A staged update used to
  say only "Update Ready to Install!" (and "Ready!" on the board), so which
  build would be applied was not visible. The Settings card, the top-of-page
  banner, the navbar indicator, and the board's Updates / Install Pending rows
  now include the pending version, matching the "Update Available: v…" line
  that already named it before the download.

- **An engine profile is named after what it sets**: a profile is a section in
  the engine's `.uci` file, and its name used to be four things at once -- the
  profile's identity, the value the player-strength and Original Centaur settings
  store to point at it, the address the editor saves and deletes through, and the
  text shown in every picker. Every awkward rule in this area was the price of
  that: `Default` could not be edited because the seed owns the name, saving asked
  whether to rename a rung whose Elo had moved, a profile whose name differed only
  in capitalisation from another silently overwrote it, and a rename left the
  settings that referenced it naming nothing. A profile now has a generated
  identity of its own, and its label is composed from its own option values -- so
  `1600 ELO` is read back from the `UCI_Elo` the profile sets, and a Maia rung
  from the net it selects, with the Elo stored once and nothing to drift out of
  step with it. Naming a profile is optional and is an ordinary edit like any
  other value: a name is shown in place of the composed label, clearing it returns
  the profile to that label, and neither can strand a setting, because no setting
  refers to a profile by name. Which values compose the label is declared per
  engine and can be narrowed per install (the `ProfileLabel` key in the `.uci`
  file's `[DEFAULT]`), with unknown keys ignored and a guard that falls back to
  the declaration rather than labelling a profile with, say, its hash size.
  Existing configuration keeps working untouched: a strength stored under an old
  name still resolves, and "Reset profiles" moves an engine to the new layout.
  Editing `Default` now creates a new profile rather than refusing, since the
  identity it needs is minted rather than typed.

- **Engine profile settings save as they are changed**: the profile editor was
  the last page holding edits behind a button, so a strength changed and left
  unsaved played the old value with nothing on screen saying so, while every
  value on the Settings page and every board menu already persisted as it was
  set. An edit to an existing profile is now written shortly after it stops
  changing, which makes dragging a slider one write rather than one per step. A
  cleared number is left unwritten until it has been retyped, because an empty
  field means "use the engine's own value" and saving it mid-retype would drop
  the setting the profile exists to make. Creating a profile -- including editing
  `Default`, which forks one -- keeps its button, since each press mints an
  identity and so is the one save that cannot be repeated harmlessly.

- **Engine list groups and orders itself by rating**: The Settings engine list
  puts each engine in a strength group and, within it, lists the strongest
  first. Both now follow the engine's published rating, recorded once in the
  engine catalog and sent to the page with the rest of its details.
  - Reckless, the strongest engine in the catalog, was listed under Specialty
    alongside the deliberately weak engines, because the page decided the groups
    from lists of engine names written into the page itself and anything absent
    from them fell through to Specialty. It now leads the Top Tier group.
  - Group membership is a rating band (3300+ for Top Tier, 2900+ for Strong),
    so adding an engine to the catalog files it correctly without touching the
    page, and the two can no longer disagree about where an engine belongs.
  - The page also stopped recognising the engine that ships with the system by
    name, and asks whether it is a system package instead, which is what the
    "System" badge and the absent Uninstall button actually depend on.

- **One Settings order for the board and the web**: The web's Settings tabs were
  ordered by a list written into the page, while the board's Settings menu was
  ordered by the shared menu catalog. Two lists for one decision, and they had
  drifted: Agents appeared third on the board and seventh on the web. The web now
  takes its tab sequence from the catalog it already fetches, so the order exists
  in one place and a change moves both surfaces together.
  - Agents moves on the board to sit after Engines, matching where the web had
    it, since the web's order was the one being kept.

- **About opens the board's System screen, as it opens the web's System tab**:
  The web's System tab leads with the device's version, hardware and memory; the
  board listed the same information fourth, between Reset Settings and Power.
  Sharing the catalog fixed the order of the Settings sections but not the order
  inside them, and System was where the two had drifted. About is now the first
  System row on the board, and the rows the two surfaces share appear in the same
  sequence on both.
  - The web's Connectivity cards now come from the same catalog container the
    board's Connectivity menu is built from. The order they were written in
    happened to match, but nothing held them together, which is how System
    drifted. WiFi, Bluetooth, Chromecast and Accounts are unchanged.

- **The whole frontend lint is a gate**: `npm run lint` reported 44 errors and
  was allowed to, because only a security-rule subset of it blocked a commit or
  a build. The subset was drawn when the rest had a backlog; that reasoning made
  the backlog permanent, and a green commit said nothing about the rules that
  were finding real defects. The backlog is cleared, the subset config is gone,
  and the pre-commit hook and CI both run the one full ruleset, failing on
  warnings so a suppression cannot outlive the finding it was written for. Three
  suppressions remain, each on a load-once-on-mount effect, where the rule
  reports a call it cannot follow past its first await.
  - Nine callbacks that retry after a login referred to themselves through the
    `const` they were being assigned to, a read inside the callback's own
    initializer that would have thrown had it ever run during the assignment.
    They also cost the compiler its analysis of the surrounding component, which
    is what produced most of the rest of the report.
  - The move list in Analysis, the optimistic piece in the live board and the
    chosen month in Games were each corrected by an effect after the render that
    got them wrong, so an arriving move was painted once against the previous
    list, and a piece stood on its optimistic square for a frame after the board
    had said otherwise. All three are settled during the render that first sees
    the new data.
  - The coach panel keeps its remarks in state rather than a ref and works out
    what to show from the move being viewed, so a coached move appears without a
    loading line and a failure is no longer shown against the next move while it
    loads. Its behaviour had no test; it has seven now.
  - The API and login dialogs stayed mounted while closed and cleared themselves
    from an effect on reopening. They exist only while open, so each opening
    starts fresh.
  - The account-slot rules and the catalog row renderer moved out of the pages
    that also export components, which restores Fast Refresh for those files,
    and the duplicate account record type in Connectivity now comes from the one
    definition.

- **Positions is a main-menu entry on the board**: Choosing a position starts a
  game from it, but the board listed it among the device settings while the web
  has always given it a page of its own, reached from the main navigation. The
  board now lists Positions in its main menu, between PLAY and Original Centaur,
  and Settings no longer offers it. Every Settings entry that remains backs a web
  tab.
  - Positions, Original Centaur and Settings are now the same height below PLAY,
    which stays the largest row on the screen. The main menu allocates height by
    weight, so Settings gave up its extra height to pay for the new row and all
    four still fit without scrolling.
  - Leaving a position game reopens the Positions list as before; backing out of
    that list now returns to the main menu rather than into Settings.

- **Board and web show the same engine list**: Each surface used to build its own
  list from the shared catalog, so every rule about how the list is presented was
  written twice and only some rules got written twice. They now render one
  view-model, the way both already render one menu catalog.
  - The board groups engines by strength and lists the strongest first, instead
    of sorting installed-first and then alphabetically. The order no longer
    depends on what happens to be installed, so two boards show the same catalog
    the same way.
  - An engine this device cannot build is greyed out on the board with the reason,
    rather than offering an Install that is refused the moment it is pressed.
  - Engines added by the operator now appear on the board, under their own
    heading. Previously an engine uploaded from a phone was invisible on the
    device it was uploaded for.
  - An installed engine missing its companion weights says "Needs repair" in the
    list, and a stopped install shows how far it got, so neither has to be
    discovered by opening the engine.

- **Project Structure**: Complete reorganization
  - Source code moved to `src/universalchess/`
  - Build scripts moved to `scripts/`
  - Packaging files in `packaging/`
  - Development tools in `tools/`

- **Package Architecture**: Changed to `all` (architecture-independent)
  - Python code works on both armhf and arm64
  - Engine binaries handled separately

- **Entry Point**: Renamed from `universal.py` to `main.py`

- **Board Controller**: Explicit initialization instead of import-time
  - Better test isolation
  - Cleaner startup sequence

- **Starting the board is a function, not a side effect of importing it**:
  `main.py` began the product while it was still being imported -- the previous
  shutdown audit, resource loading, the e-paper controller probe and the board
  handshake were all top-level statements. Importing the entry point therefore
  booted the board, so no test could import it: the file held the main loop, the
  menu dispatch and the board-command routing, and every test that needed one of
  them either duplicated the logic or asserted against the source text. Those
  steps now live in `app/bootstrap.py:boot()`, and `main.py` is a dozen lines
  that call it and then run the application (`app/board_app.py`). The order
  boot() runs them in is the order the hardware requires, and a test now holds
  that order, which nothing checked while it was a run of statements.
  - The panel bring-up (controller probe, waveform profile, `[display]`
    settings) moved to `app/display_boot.py`, and the previous-shutdown audit to
    `board/boot_report.py`. The About screen read that verdict by importing the
    entry point mid-render, which was the one runtime import of `main` in the
    tree -- and would have booted the board from inside a widget had the module
    not already been loaded.
  - The startup splash is now shared through `app/startup_splash.py`, so the
    slow imports still name what they are waiting on while the panel is up.
  - The Bluetooth stack (`bluetooth`, `gi`, `dbus`) is stubbed for tests
    alongside the SPI and GPIO modules it sits beside, since it is the same kind
    of dependency: a host facility that is absent off a Pi and mocked by every
    test that reaches it. With that, the application module imports in a test in
    a fifth of a second.
  - Live display tuning now reads the Manager from the board module rather than
    from a variable captured at startup, so a panel that only came up on the
    late-initialization path is tuned as well.

- **The labels and readings the two apps share are computed in one place**: the
  board and the web each had their own copy of the `[display]` settings reader
  and of the coach's language reader, byte for byte, so a change to one was a
  silent divergence between the screen and the browser. Both now call one
  function -- `board/display_settings.py` and
  `language_service.current_coach_language_name()`. The labels the board draws
  from its own settings (the two Time Control rows, the engine picker row, the
  player summaries, a player's default name and the strength an engine's stored
  section actually plays) moved out of the application module to the modules
  that own the data, taking their settings as arguments instead of reading a
  singleton, and are tested directly for the first time. Telling both players
  that a side resigned is now the player manager's `on_resign`, beside
  `on_takeback`, rather than a closure written out once per resign gesture.

- **The two player slots were named by three separate copies of the same
  string**: `"PlayerOne"` and `"PlayerTwo"` name the sections every board's
  `centaur.ini` stores its players in, and the board, the web app and the Lichess
  account store each spelled them out for themselves. A rename would have reached
  some readers and not others, which reads on the board as players that revert to
  defaults. They now live beside the dataclass that reads them, with a test
  asserting the literals rather than only the symbols, because the text is the
  contract with configs already on disk.

- **A board key that was handled could still count towards recovery**: five
  consecutive presses that reach nothing mean the board has stopped routing keys,
  so it tears down the game and returns to the main menu on its own. The counter
  was cleared in nineteen separate branches of the key router, and a branch that
  handled a key without clearing it let the count climb through presses that were
  working -- so the fifth one abandoned a game that was playing perfectly. Handling
  a key and recording it are now one step, and the priority order the router
  follows (shutdown, held OK, overlays, then the screen) is stated once instead of
  being the order of nineteen blocks.

- **A subsystem that refused to stop could cost the battery**: shutdown released
  eleven subsystems, each with its own copy of "call the teardown, log whatever it
  raised". The steps after a failure include putting the controller to sleep, and a
  controller left awake keeps drawing from the battery for as long as the board is
  off -- a board that will not start days later, with nothing on screen to say why.
  Every step now runs regardless of the ones before it, the sleep command is
  reached even when the power-off beep or the LED cascade fails, and every failure
  is reported rather than only the first.

- **A game's handles are discarded together or not at all**: the protocol,
  display, controller, coach and Lichess session that exist only while a game is
  being played were five module-level names, released by a teardown that cleared
  each one separately. Nothing checked that it reached all of them, and a handle
  it missed survived into the next game -- drawing on a display that game does not
  own, or routing board events into a game that has already ended. The order also
  mattered and was recorded nowhere: the Lichess session holds the started-splash
  timer, so a game that ended within five seconds of starting could paint game
  widgets over the menu that replaced it. Both are now stated once, and the
  handles return to their defaults even when a component's own cleanup raises,
  which previously abandoned everything after it.

- **The application's own state has owners, and each one is tested**: the board
  held its state as seventy module-level names, so behaviour that existed only as
  a rule about them -- which overlay gets a key first, that a stop must set two
  flags, that five unanswered presses mean the board is stuck -- could only be
  read by reading the 8,000-line application and could not be tested at all. The
  overlays that consume keys (help tip, error splash, keyboard, pairing prompt)
  are now `app/modals.py`, with their priority order stated once instead of being
  the order of four `if` blocks. `running` and `kill` become
  `app/lifecycle.py`, one flag with a reason, which also carries whether the stop
  was a power-off and guards teardown against running twice; one caller used to
  set only `kill`. The unanswered-key counter that recovers a wedged board is
  `app/key_recovery.py`. Which engines can be played and how strong each can be
  set moved to the engine modules that own them, cache included, so the web can
  ask the same questions. Turning a player slot's settings into a player is
  another: it was a closure inside the 750-line game builder, so nothing checked
  that an unnamed engine carries its strength label into the PGN, that a novelty
  engine such as Worstfish runs its policy over the shared Stockfish instead of
  starting a second one, or that a player type left behind by a downgrade still
  yields a player rather than a side that can never move. It is `players/factory.py`
  with eighteen tests. Which screen is showing is one more: "a menu is on
  screen" was written eight times as `state == MENU or state == SETTINGS`, so a
  screen added without being added there reads as "in game" and sends board keys
  and piece lifts to a game that is not showing. Named once in `app/session.py`,
  with a test that fails if a screen is left unclassified.

- **The main loop decides what to do next, and the decision is tested**: twelve
  conditions are consulted on every pass -- five kinds of deferred repair while a
  game runs, seven things to settle before the menu is drawn -- and they were two
  `if`/`continue` ladders inside the loop. Their priority was whatever order the
  branches happened to be in, and nothing established that a condition losing a
  pass was still waiting on the next one, which is the half that fails quietly: a
  claim that discards what it did not act on loses a web settings change made in
  the same moment as a game rebuild, and draining the queued piece events without
  entering the game throws away the lift half of the user's first move. Both
  choices are now functions of the pending state (`app/game_step.py`,
  `app/menu_step.py`) that claim exactly one condition, with every pairing of the
  twelve asserted in both directions. Turning the path saved at shutdown into the
  screen to reopen had drifted into two copies of the same classification, one of
  which handled an empty saved path and one of which would have raised on it
  during startup, before the display exists to report it; they are now one.

### Removed

- **Dead `GET /api/engines`**: superseded by `/api/engines/all`, which the web app
  actually calls, and unused by anything for long enough that it still listed
  every catalog engine while omitting operator-added ones. Its failure path
  returned a hand-written Stockfish entry, reporting an engine as installed
  without having checked -- exactly the fabricated fallback that is worse than an
  error.

- **Deprecated Engines**: Fire, Laser (x86-only, incompatible with ARM)
- **Legacy CI**: Docker-based cron CI system (moved to `.github/legacy-ci/`)
- **Obsolete Tests**: Removed outdated promotion hardware tests
- **Legacy shutdown-time updater**: `scripts/update.sh` shipped to every board and
  was reachable by nothing: it installed `dgtcentaurmods_armhf.deb`, a package
  this project does not build, from an update-on-shutdown flow that Settings ->
  System replaced. Its last line started the controller sleep hook, so the file
  was also a stray way to power the board off.

### Fixed

- **Centered e-paper text with a line break sat the short line off-center**:
  ``Overflow.FIT`` keeps wrap off when each explicit line already fits, then
  painted the whole string in one ``draw.text()``. PIL left-aligns
  newline-separated lines to the same x, so splash copy such as
  "Loading" / "Challenge..." (and "Waiting for" / "opponent...") put the
  short line under the left edge of the long one. Each line is now given
  its own origin, the same path word-wrap already used.

- **Web Lichess lobby hid nothing when no accounts were saved**: Settings →
  Players still drew Rated, Ongoing Games, Challenges, and Seek New Game
  against an empty store, so the card offered play controls that cannot
  run and a no-token empty state that told the user to add an account
  while Accounts was already on the card. Those rows now appear only
  once a Lichess login exists. A failed or unauthorized account list is
  not treated as empty, so the rows are not buried behind a false
  "add an account" state. The board lobby is unchanged.

- **Display tuning disappeared when the panel was the thing that needed
  tuning**: the Settings card hid itself unless the board had already
  reported an initialized controller, so a failed init, a board still
  coming up, or a GET that did not land left no way to pick a waveform
  for the next boot. The card is always shown. With no live controller
  it lists every profile (UC8151D and SSD1680) and saves the selection
  for the next start; live apply remains best-effort when the board is
  running.

- **`deploy-to-pi.sh` aborted with "Cannot reach" on a board that accepted
  password SSH**: the sudo-NOPASSWD probe uses `BatchMode=yes`, which
  exits 255 both when the host is down and when the key is not in
  `authorized_keys`. Interactive `ssh pa@orangepizero2w.local` prompted
  for a password and logged in; the deploy treated that 255 as
  unreachable and never offered a prompt. A BatchMode 255 whose stderr
  contains `Permission denied` now takes the existing TTY staging path
  so rsync and `ssh -t sudo` can ask for a password. Connection timeout,
  refused, and host-key failures still abort.

- **Installing the package on Armbian failed at "Installing python
  packages"**: the postinst builds `/opt/universalchess/.venv` with
  `python3 -m venv`, but nothing declared `python3-venv`, which is where
  Debian keeps `ensurepip` and the bundled pip wheel a new environment is
  seeded from. Raspberry Pi OS ships it in the base image, so the omission
  was invisible there; on an Armbian trixie image the step aborted with
  "ensurepip is not available", the postinst exited non-zero, and
  universal-chess was left half-configured with no venv for the service to
  start from. `python3-venv` is now a declared dependency, unversioned so it
  tracks whichever interpreter the Debian release makes default.

- **An install that failed once could not be repaired by installing what it
  was missing**: the postinst skipped building `.venv` whenever
  `.venv/bin/python` existed, but `python3 -m venv` links the interpreter
  into place before it runs ensurepip. A create that aborted at ensurepip
  therefore left a directory that satisfied that test permanently, and every
  later install skipped creation and died one line further on with
  `.venv/bin/pip: No such file or directory` -- with nothing to do but delete
  the directory by hand. Creation is now decided by both tools the install
  actually runs out of the environment, the interpreter and pip, so a
  half-built venv is completed on the next install instead of being mistaken
  for a working one.

- **`<hostname>.local` did not resolve on Armbian, so the web UI was
  unreachable under the name its certificate covers**: nothing declared an mDNS
  responder. Raspberry Pi OS ships `avahi-daemon` in its base image, so the
  omission was invisible there; Armbian Minimal carries only essential packages
  and does not include it, leaving a board that answers on its address but to no
  name. The consequences reach past convenience: the server certificate's
  Subject Alternative Names are derived from `<hostname>.local`, so it was
  issued for a name no client could look up, and the install procedure directs
  users to `http://<hostname>.local/` for the web interface. `avahi-daemon` is
  now a declared dependency, which apt installs on Armbian and which is already
  satisfied on a Raspberry Pi.

- **Orange Pi Wi-Fi status and Scan used Raspberry Pi OS tools Armbian
  does not ship**: the web UI and e-paper reported disconnected (and Scan
  returned nothing) while wlan0 was associated, because status called
  `iwgetid`/`iwconfig` and the privileged helper ran `iwlist`. Armbian
  puts `iw` in `/sbin` and does not install `wireless-tools`. Status now
  reads `iw dev wlan0 link` (and finds `rfkill` in sbin), and Scan falls
  back to `iw dev wlan0 scan` and parses BSS blocks. `iw` is preferred, not
  required: wireless-tools remains the fallback for the SSID, signal and band
  wherever `iw` is absent or answers incompletely, so the substitution cannot
  turn into the same defect pointed the other way. Signal strength on both
  images is now the one dBm-to-percent mapping (-90 dBm = 0%, -30 dBm = 100%)
  the `iwconfig` path used, instead of each caller deriving its own.

- **A hard-blocked Wi-Fi radio was shown as enabled**: the rfkill check looked
  only at the soft block, so a radio blocked in hardware read as on. The status
  bar showed Wi-Fi available and every connect attempt failed with nothing to
  explain it. Both block kinds now count as disabled. In the other direction,
  a board where the block state cannot be determined at all -- no `rfkill`
  binary, or no rfkill entry for wlan -- is reported enabled rather than
  disabled: neither of those says the radio is switched off, and whether the
  board has a radio is answered separately by the wireless-capability probe.

- **Orange Pi Wi-Fi Connect used NetworkManager on an image that has
  none**: Scan and status work via `iw`, but Connect still called `nmcli`.
  This Armbian image uses systemd-networkd plus wpa_supplicant, configured
  by netplan. NetworkManager is not installed and must not be: it fights
  networkd. The privileged helper now writes
  `/etc/netplan/60-universal-chess-wifi.yaml` and runs `netplan apply`
  when `nmcli` is absent (passphrase on stdin, never on argv). Raspberry
  Pi OS still uses `nmcli`. Saved and Forget use the same netplan file.

- **Orange Pi reported a V2 e-paper panel when none was present**: the
  libgpiod backend claimed BUSY as a floating input. Allwinner GPIO inputs
  default to pull-up, so a disconnected pin reads HIGH, which the UC8151D
  driver treats as idle and reports as a working V2 panel. A fitted V1
  panel already overrode that pull-up by driving idle LOW, so the UC8151D
  probe timed out and SSD1680 was selected; the false V2 is the undriven
  pin, not a fitted idle V1. After a no-panel boot the status file then
  hinted UC8151D, so the next probe started on V2. The Pi path already
  pull-downs the same line (`gpiozero.InputDevice(pull_up=False)`);
  libgpiod now requests `Bias.PULL_DOWN` so a floating BUSY reads LOW and
  the UC8151D probe times out instead of succeeding.

- **An empty e-paper connector was reported as a working panel**: `init()`
  succeeded whenever BUSY was already at that driver's idle level. Neither
  controller is readable over SPI, so the wait never required the pin to
  move. A disconnected BUSY with pull-down sits LOW (SSD1680 idle), so both
  a Pi and an Orange Pi after the pull-down alignment reported V1
  "Panel initialized and responding." A disconnected pin with pull-up sits
  HIGH (UC8151D idle) and reported V2. The first `init()` on an instance
  now requires BUSY to leave idle after the command that must busy a fitted
  panel (UC8151D POWER ON, SSD1680 SWRESET, IL3820 power-on). An empty
  connector fails that probe; a later `init()` on the same instance (live
  waveform change) does not, because POWER ON on an already-powered panel
  may not pulse BUSY.

- **A BUSY-timeout on the System card guessed a cause**: the UC8151D wait
  reported "panel unresponsive or incompatible (e.g. inverted BUSY
  polarity)" and the SSD1680 wait named "not an SSD1680/IL3820-family
  panel". Neither is observed -- those are hypotheses for why the pin
  stayed busy. The messages now state only that BUSY was not released
  within the wait.

- **Orange Pi e-paper failed with "No module named 'gpiod'" after a
  successful install**: the GPIO dependency was declared as
  `python3-rpi.gpio | python3-libgpiod`, and apt takes the first alternative
  when neither is installed. On Armbian, where neither is present, that
  installed rpi.gpio and never libgpiod -- the one package the Orange Pi
  e-paper backend imports. The clause had been written believing Armbian could
  not install rpi.gpio at all, but Debian trixie carries it for arm64, so the
  alternative resolved the wrong way on exactly the board it existed for.
  libgpiod is now listed first, which installs it on Armbian and still leaves a
  Raspberry Pi on the rpi.gpio its base image already has, since an installed
  alternative satisfies the clause on its own.

- **Orange Pi Zero 2W board service crash-looped on `import RPi.GPIO`**:
  `epd2in9d.py` still imported `RPi.GPIO` at module load even though every
  pin and SPI call goes through epdconfig. Raspberry Pi OS ships that
  package; Armbian does not. The leftover import is gone, so the libgpiod
  backend can load.

- **Orange Pi Zero 2W e-paper missed the live spi-gpio master**: userspace
  looked for a sysfs driver named `spi-gpio` (the DT compatible). This
  kernel registers the platform driver as `spi_gpio`, so `module_init`
  raised "overlay not loaded" while `/dev/spidev0.0` was already the
  overlay child. Both names are accepted. `module_init` also releases a
  prior libgpiod request before claiming again, because display boot and
  `board.init_display()` both call it on the same singleton; the second
  `request_lines` was EBUSY (lines 76/233/267 already `universalchess-epaper`)
  and crash-looped the service.

- **Orange Pi Zero 2W opened the chess UART at 750 kbaud**: the Centaur MCU
  is 1 Mbps. H618 APB2 boots from OSC24M, so 8250's integer divisor for
  1 Mbps is 2 (24 MHz / 16 / 2 = 750 kHz). Discovery transmitted (`tx`
  climbing) and never heard the board (`rx` stayed 0). The spi-gpio
  overlay now also parents APB2 from PLL_PERIPH0 at 300 MHz (divisor 19,
  986842 baud, 1.32% error). The 50 MHz rate in Allwinner's UART note is
  4.17% and sits on the 8N1 budget. Needs a reboot for the overlay.

- **Orange Pi Zero 2W e-paper nodes were root-only**: after the spi-gpio
  overlay loaded, `/dev/gpiochip*` and `/dev/spidev0.0` stayed `root:root`
  mode 600. The service runs as the UID 1000 user, so libgpiod and spidev
  could not open them. The package now creates `gpio`/`spi` groups, ships a
  udev rule that sets those nodes to `0660`, and applies the same ownership
  during configure.

- **Timed games hid the clock when Show Clock was off**: Show Clock is the
  untimed turn-indicator toggle. The same flag also hid the e-paper clock in a
  timed game, so remaining time vanished and the layout still reserved a blank
  band. Timed games now always show the clock. Show Clock still hides the turn
  indicator in untimed games, and its help names the exception.

- **Original Centaur import rejected any SD card that was not cleanly shut
  down**: the image is loop-mounted read-only, and a card pulled from a board
  that lost power rather than being shut down carries uncommitted ext4 journal
  entries. ext4 will not mount such a filesystem at all until it has replayed
  them, and replaying is a write, which a read-only loop device cannot accept --
  so the mount failed with "write access unavailable, cannot proceed" and the
  import stopped at "Failed to mount the uploaded image." The mount now passes
  `noload`, which skips the replay. Files written in the moments before power
  loss can therefore read as stale, which does not affect the long-installed
  engines, fonts and books the import copies; the option only removes a write,
  so the image is still never modified.

- **A failed Original Centaur import left almost nothing to diagnose it with**:
  the privileged mount, stage, unmount and armhf helpers all ran with their
  output captured and then discarded once the exit code was read, so a missing
  sudoers grant, an SD app directory the service user could not read, a busy
  mountpoint and a stale apt index all arrived as one fixed sentence. Anything
  that was not a helper -- a browser upload cut short, a card with no room for
  the 2 GB decompression -- was caught by the worker's catch-all, stored as the
  words "Import failed", and written in full only to ~/debug.log, which is
  truncated on every boot and so was usually gone before the failure was
  reported. Settings -> System -> Event Log now carries the whole run under a
  Centaur import heading: the upload it accepted and its size, every stage as it
  starts, and for each failure the command, its exit code (or that it timed out,
  or could not be started at all) and the tail of what it printed, collapsed to
  one line and bounded so an apt run cannot evict the rest of the board's
  history. Failures that reach the user keep their author-written, path-free
  message, because that one is returned over HTTP; the detail belongs in the
  auth-gated log. Rejected uploads, a save that runs out of disk, an import a
  restart killed and the percent it had reached are recorded too, and a
  successful import files the file count and how long it took, so a slow one has
  a baseline to be compared against. Three steps that used to fail as the
  generic "Import failed" -- the decompression, the copy into the install
  directory, and the engine-proxy hook -- now name their own step and next
  action.

- **Original Centaur in translate mode crashed on a battery poll**: the T5D
  driver calls ``image.tobytes()`` in ``update()`` without checking that the
  framebuffer exists. Translate mode's GPIO shim makes the first paint slower
  than native serial, so a battery event from the board could reach
  ``event_battery_`` before that paint and dump a Python traceback onto the
  panel -- not every launch, only when the race lost. The serial tap now holds
  board-to-Centaur bytes until the display gateway has rendered one frame (or
  ten seconds, so a silent gateway cannot deadlock the link). Direct mode is
  unchanged.

- **Original Centaur crashed with any engine other than Stockfish**: Centaur
  always sends Stockfish ``setoption`` lines (Hash, MultiPV, Skill Level) and
  was written against Stockfish's stdout. Other engines print a banner or reject
  unknown options and the session died. The UCI proxy now forwards only lines
  that are UCI, and drops ``setoption`` names the engine did not advertise in
  its ``uci`` handshake. Stockfish still receives Hash (memory-capped) as
  before.

- **Original Centaur never moved when the engine was not Stockfish**: dropping
  banners stopped the crash, but Centaur's bundled python-chess 0.x still saw
  the real engine's option list (CT800 advertises combo/string/button names)
  and ``magic_choose`` still needed a MultiPV ``info`` line before it would
  play. The proxy now presents a Stockfish-shaped Hash/MultiPV/Skill Level
  handshake, inserts ``multipv 1`` on PV info that omitted it, and synthesizes
  a dummy scored PV in front of a bare ``bestmove`` (Drawfish/Worstfish print
  only that). Centaur's Skill Level setoption is still dropped on the way to
  engines that do not advertise it.

- **A PLACE with no lift put the board into correction mode**: the Centaur
  sometimes reports a PLACE with no preceding LIFT -- a reed bounce after the
  piece is already seated, a trailing duplicate after occupancy already accepted
  the move, or a ghost PLACE on an occupied square. That was formed into a
  destination-only move. Lichess rejects those unless the square is the pending
  destination, which a bounce on the source or after the turn has switched never
  is, so the board entered correction and the next real lift looked ignored. A
  PLACE that does not change occupancy, with no move in progress, is now dropped
  before it reaches the player. A real missed-lift move still vacates a source
  square, so occupancy no longer matches and destination-only recovery still
  runs; putting a lifted piece back still reaches the player so the lift buffer
  is cleared.

- **A chess app connected over classic Bluetooth behaved differently from the
  same app over BLE**: what the board does when a phone app connects was written
  twice -- once for BLE, once as a copy for RFCOMM nested inside startup -- and the
  two had drifted in four places, each of which only ever broke on one transport.
  Over RFCOMM the menu position was not snapshotted before the game started, so
  suspending that game reopened the top of the main menu instead of the submenu the
  user had been in; the board was never handed over when the app arrived while a
  game was still being built, so it went on playing locally and ignoring the app;
  and the controller was never told when the app disconnected, so it kept routing
  moves to a link that was gone and pieces moved on the board did nothing. Over
  BLE the link was never recorded in the live Bluetooth status, so a connected BLE
  app read as "not connected" on the board and the web, with no emulator or
  connected-since time, and the advertising indicator could not report that
  advertising had paused because a client was connected. Both transports now share
  one pair of handlers, and every routing test runs against both, so a difference
  between them fails instead of waiting to be noticed.

- **Turning off classic Bluetooth said nothing about it**: starting the board with
  `--no-rfcomm` skipped the RFCOMM server through a branch with no log at all, so
  its absence was indistinguishable from a server that had failed to start. BLE
  reported both of its reasons; RFCOMM reported only the missing-controller one.
  The decision of what to bring up is now made in one place from the hardware and
  the flags together, always with the reason it was skipped, and a test covers
  every combination of the two flags and the controller -- including the invariant
  the branch existed for, that a board with no Bluetooth controller attempts none
  of it and so cannot burn its only core retrying against a missing adapter.

- **A request made from the board's serial, Bluetooth or web thread could be
  silently dropped**: eleven kinds of work are deferred from those threads to the
  main loop, because only the main loop may rebuild widgets or restart players.
  Each was a module-level flag the loop tested and then cleared as two separate
  statements, so a request that arrived between them was erased -- the work never
  ran, and nothing recorded that it had been asked for. From the board it looked
  like a press or a web change that was simply ignored: a settings change made
  while a game rebuilt, a rebuild requested during another one, a BACK on the
  Lichess waiting splash. Testing and clearing is now one locked operation, so a
  request either belongs to that pass of the loop or waits for the next, and a
  test holds the interleaving that used to lose it.

- **The unclean-shutdown warning was drawn on a screen nobody could reach**:
  every boot audits the OS logs for evidence that power was cut before the
  filesystem finished unmounting, and the verdict was displayed by an About
  *widget* that the menu never opened -- the About screen has been menu-driven
  for some time, and the widget survived as the only runtime importer of the
  entry point. The warning now appears where the audit's readers look, as a row
  under About beneath the telemetry, translated in all five languages, and the
  orphaned widget is gone.

- **The WiFi, Bluetooth and Chromecast menus could not open**: three of the
  board's menus reached their status modules through a hand-written
  `__import__("DGTCentaurMods.epaper.wifi_info", ...)`, naming the package as it
  was called before the rename. A string import is invisible to every tool that
  follows imports, so the rename left them behind and nothing reported it: the
  menus raised `ModuleNotFoundError` the moment they were opened. They import
  their modules directly now, and the WiFi and Bluetooth status rows have tests,
  which is what would have caught this. No string imports remain in the
  application module.

- **Engines were named inconsistently, and sometimes not by their name**: the
  Settings > Players row derived an engine's label by capitalising its id, and
  the per-player row showed the id raw, so the same engine read as `Ct800` on
  one screen and `ct800` on the next -- neither of which is its name, which is
  `CT800`. An engine added by the operator had no name on either screen. Both
  rows now ask for the engine's display name, from the catalog or from the
  custom-engine registry, and share one summary function so they cannot drift
  apart again.

: the help read "The AI coach's remarks use the separate Coach Language
  setting", sending the user to look for a screen that is not in the menu -- and
  stating the opposite of how the board works. The coach follows this very
  setting: both the board and the web API derive the coach's language from the
  device locale, and the catalog has only one language node. The help now says
  so, in English and in all three translations, which had faithfully carried the
  false statement into Spanish, French and German. A test now holds the
  behaviour the corrected help promises, so the claim cannot drift from the code
  again.

- **Translated help quoted words the screen never shows**: Alerts > Queen Threat
  explains the warning by quoting what the panel draws, and the panel drew
  English when the Spanish and French overlays were written. Localizing the
  alert changed the panel to TU DAMA, VOTRE DAME and IHRE DAME while all three
  help texts still said YOUR QUEEN, pointing at wording that no longer appears
  anywhere. The help now quotes the alert in the language it is drawn in. A
  coverage audit cannot see this class of drift -- both strings are translated,
  just no longer to each other -- so a test now checks each locale's help
  against the alert it quotes, and the Play tile's help against the label the
  tile carries once a game is under way.

- **German tagline differed between the board and the web app**: the splash and
  the web header show the same line, but the German bundles rendered the proverb
  two ways ("Dem geschenkten Gaul ins Maul geschaut" against "Dem geschenkten
  Pferd ins Maul schauen"). Both now read the Gaul wording the proverb uses, as
  Spanish and French already matched across the two bundles.

- **Web app Reload did nothing when a new version was waiting**: The "A new
  version of the app is available" banner posted ``SKIP_WAITING`` to the
  service worker and then waited for ``controllerchange`` before calling
  ``location.reload()``. iOS Safari (and some kiosk Chromium builds) never
  fire that event, or ignore reload outside a user gesture, so the button
  appeared inert and a second tap was discarded by an in-flight flag. Reload
  now navigates in the same tap. Auto-apply still waits for the new worker,
  with a short fallback, and surfaces the banner if that reload is blocked.

- **Move list appears with analysis off**: UP/DOWN during a game highlighted
  a played move in a list that lived inside the analysis widget, so turning
  Show Analysis or Live Analysis off hid or never created it and the arrows
  did nothing. The move list is now its own widget, always built for a game,
  and UP/DOWN always pages it. Wrapping home restores the board; the eval
  panel only comes back when Show Analysis is on.

- **Lichess clock sat ahead of the browser**: Remaining from the Board API
  is a snapshot at that instant, and Lichess starts White's clock when the
  game starts. The board applied that snapshot then left the countdown
  stopped until the first turn, and painting the deferred game widgets
  reseeding the spec's initial time over it. The e-paper froze at 30:00
  (or jumped back to it) while the browser counted down. Remaining now
  starts the timed clock, and widget paint keeps the snapshot.

- **Lichess games used the Game-menu clock**: Start configured the e-paper
  from the local 30 min / 1 min / … control even when a Lichess player was in
  a slot, and remaining updates were applied only when ``wtime``/``btime``
  were millisecond ints, so berserk's ``timedelta`` on later ``gameState``
  events never snapped the widget. Correspondence unlimited
  (``2147483647`` ms) then ran that local clock to flag. ``gameFull.clock``
  now installs the Lichess Fischer pair (or untimed correspondence) before
  the widgets are built, and remaining is applied from both encodings.

- **Joining a Lichess game in progress left the pieces at the opening**:
  The first Board API snapshot for an ongoing game carries every move already
  played. The board treated that as one new remote ply to copy from the
  start position, so the logical game stayed in the opening, the last move
  was either ignored (if it was ours) or lit as a forced move that is not
  legal from there, and correction never asked for a setup. Correspondence
  rejoin after our own last move is the one-ply form of the same hole: that
  UCI was classified as an echo and dropped, so the e-paper stayed at the
  opening and later opponent plies were applied from there. The first
  snapshot now replays the whole list onto the live game and enters
  correction, the same guidance resume uses for a saved position. A single
  new opponent ply during a live game is still one forced move.

- **BACK on Waiting for game left the Lichess seek listed**: The splash
  called stop, but ``board.seek`` is a streamed POST that Lichess treats as
  the live seek until the connection closes. stop() only set a flag and
  joined that thread, so the socket stayed open and the lobby still showed
  the seek. Two later attempts to close it did nothing either: closing the
  ``requests`` session only clears connections sitting idle in the pool, never
  the seek connection that is checked out and blocked in a read, and the
  client the close was asked of exposes no session at all -- berserk keeps it
  on a private requestor, so the call resolved to nothing. Closing the
  response object instead deadlocks against the blocked read. stop() now shuts
  down the sockets of the streams it opened, which both wakes the reading
  threads and sends FIN so Lichess drops the seek; the session is kept
  alongside the client so teardown can find it. BACK is also wired before
  players start, so the key is not swallowed while the splash is already on
  screen and start() is still authenticating. BACK that arrives before
  GameManager exists is recorded so the seek is never posted.

- **A remote Lichess abort left the board in a live game**: When the opponent
  aborted, the Board API streamed status ``aborted``. The player treated that
  as no PGN result and skipped the game-over callback, so the clocks kept
  running and the Lobby / Seek New Game / Cancel menu never appeared. Abort
  (and noStart) now end the game with result ``*`` and offer that menu, the
  same one a board-reset during a Lichess game already uses.

- **The next-game menu after a Lichess abort asked to seek**: That shared menu
  always used the board-reset header "Seek a new game?", so an abort looked
  like a prompt to start another game and never said why the current one
  stopped. Abort now heads the menu with Game aborted, noStart with Never
  started. Setting the pieces back to the start still asks to seek, because
  that gesture is the user's.

- **A remote Lichess resign left the next-game menu closed**: Abort already
  opened Lobby / Seek / Cancel with the reason on the top row. Resign (and
  mate, timeout, draw) only painted the game-over overlay, so after the
  opponent resigned the board sat on that overlay with no way to seek or open
  the lobby. Those endings now offer the same menu, headed Opponent resigned /
  Checkmate / Out of time / Game drawn.

- **A random Lichess seek said nothing about colour**: The seek passed
  ``color=None`` for a random game, and ``requests`` drops None form fields, so
  the request reached Lichess with no colour at all and depended on the server
  defaulting an absent parameter. ``random`` is a colour Lichess accepts in its
  own right, and is now what the board sends, so the seek states what was
  chosen rather than relying on an omission meaning the same thing.

- **Game widgets could paint over the menu after leaving a Lichess game**:
  The started splash hands over to the board five seconds after a game
  connects, on a timer nothing cancelled. A game that ended inside those five
  seconds -- an opponent aborting, or BACK into the back menu -- had already
  returned to the menu when the timer fired, and the board and info overlay
  were drawn over it. Game teardown now cancels that timer, and a timer that
  had already started firing no longer paints.

- **Lichess lobby views held their connections open until collected**: The
  Lichess Settings menu, the Ongoing and Challenges web endpoints, and Add
  Account token verification each authenticate their own HTTP session and left
  it for the garbage collector, so every menu visit, poll and token check
  stacked another idle socket to lichess.org on a board that runs for weeks.
  Each of those now closes the connection when the view it serves is done,
  including an account switch closing the connection it replaces and a failed
  sign-in closing the one it opened.

- **Lichess lobby accept left the board on the waiting splash**: After an
  opponent took the board's seek, Lichess already had a live game -- the web
  player sat on a board waiting for the first move -- but the e-paper never
  left "Waiting for game" and Human moves were never sent. ``board.seek``
  holds an HTTP stream open until Lichess closes it; after a match that stream
  often keeps sending keep-alives, and the player only looked up the game once
  seek() returned. The Board API event stream now attaches on ``gameStart``,
  and ongoing games are polled in parallel with the seek.

- **Lichess takeback left the board on the undone position**: Accepting
  a takeback updated Lichess, but the stream only treated a new ``moves``
  string as another last ply to replicate. The logical game, clocks, and
  correction LEDs stayed on the pre-takeback position. A shorter (or
  diverged) move list now pops to the remaining ply count and guides the
  pieces.

- **Lichess takeback Accept could not be chosen**: Opponent takeback (and
  draw) offers paint Accept/Decline through ``MenuManager.show_menu`` while
  the app is already in a game. Keys in that state went to the game, so TICK
  full-refreshed the panel instead of Accept, BACK opened abort/resign, and
  PLAY suspended. An in-game MenuManager overlay now receives those keys.

- **A Lichess seek is posted only from PLAY or New Game**: Starting the board
  with pieces on it, lifting a piece on the menu, connecting a BLE client, and
  resuming after a reboot all entered game mode with a Lichess slot and called
  ``board.seek`` even though the user never chose New Game. Those paths now
  attach an ongoing game if one exists and do not list a new seek. PLAY, lobby
  Seek New Game, and web New Game still seek immediately. Returning the pieces to
  the opening during a Lichess game asks what to do -- Lichess Lobby, Seek New
  Game, or Cancel, with Cancel highlighted. Cancel returns to the menu without
  seeking, and Lichess Lobby opens the lobby, because a board set back to the
  opening is as often the start of resuming an ongoing game or answering a
  challenge as it is of posting another seek.

- **Incoming Lichess challenges during a seek ask before accepting**: A lobby
  seek is the board's terms. Clicking the account in the lobby (rather than
  taking the seek) sends a challenge on the opponent's clock, rated flag,
  color, and variant. Auto-accepting those started a game the Human had not
  agreed to. An incoming challenge now shows Accept/Decline with those terms;
  Decline returns to the wait splash and the seek stays up. Taking the posted
  seek still starts the game with no prompt.

- **A challenge picked in the lobby did not join its game**: Selecting a row in
  Challenges streamed the challenge id as though it were already a game. That
  holds for an incoming challenge, which keeps its id once accepted, but a
  challenge the board sent is not a game until the other player accepts it:
  Lichess answered ``404 No such game``, the stream thread ended, and the board
  was left in a local game whose moves never reached Lichess. An outgoing
  challenge now waits for it to be accepted -- the panel says so instead of
  reading Loading Challenge -- and joins the game the moment it starts. While
  that wait is up only that challenge may be joined, so another game the account
  has running is not pulled onto the board in its place. BACK leaves the
  challenge standing on Lichess; the board did not create it.

- **Lichess match color does not rotate the pieces**: Lichess can name White
  or Black in the seconds after a match, faster than the physical board can
  be turned. Swapping the Players color control built player 1 as Black.
  Pieces stay on their physical sides: White is always player 1, Black player
  2. After the stream names the account's color, Human sits that slot and the
  e-paper rotates when that color is not the one the Players control chose.

- **The e-paper turned around for the wrong games**: The display flipped
  whenever the human was assigned Black, which is only right for a board set up
  to play White. A player who chose Black had already taken Black's side before
  the seek went out, so being assigned Black -- the color asked for -- turned
  the display to face their opponent, and being assigned White left it facing
  away from the pieces they were playing. Flip is now the disagreement between
  the chosen color and the assigned one: the display turns around when, and
  only when, Lichess hands over the other color. Chosen White and assigned
  Black flips, as before; chosen Black and assigned Black does not.

- **In-game Lichess menus stayed upright after the display turned around**:
  Flip remapped the chess squares and clock rows, but abort, takeback, draw,
  and the next-game offer (Lobby / Seek / Cancel) still painted for the
  original seat, so they were upside down from the far end. The panel now
  rotates the whole framebuffer 180 when that flip is on, and restores the
  mounting orientation when the game ends so the main menu is not left
  inverted.

- **A Lichess seek asked for no color even when one was chosen**: Every seek was
  posted as random, because the color a match names arrives faster than the
  pieces can be turned around. Rotating the e-paper instead of the pieces
  settled that, so the choice can be honored: a pairing with exactly one slot
  set to Lichess now seeks the side the human did not pick. ``color`` on a seek
  names the side the *seeking account* wants, and that account is the opponent,
  so a human who chose White posts a seek for Black and is paired as White. A
  pairing that names no Lichess slot -- a lobby Seek New Game over two engines,
  say -- has no side anyone chose for it and still seeks random, taking whoever
  answers first.

- **The Lichess lobby's New Game could start a local game instead of seeking**:
  The lobby's New Game row, PLAY pressed inside the lobby, and the web lobby's
  card all stashed a join and left the game to be built from the Players slots.
  With neither slot set to Lichess that built exactly what those slots described
  -- pressing New Game inside the Lichess menu started Player 1 against Drawfish
  and posted no seek at all -- because the seek helper refused a pairing with no
  Lichess slot, so the join was quietly dropped. A start from the lobby now
  derives its own Human vs Lichess pairing for that game alone: the human stays
  in the slot it already occupies, an absent one takes slot 1 (White's physical
  side), and the substituted Lichess slot plays as the account the lobby names.
  The saved Players settings are not written, so the next local game is the one
  that was configured. Those rows read Seek New Game on
  the board and on the web, separating them from the New Game that starts
  whichever players Settings describes. Returning the pieces to the opening
  during such a game asks the same question as any other Lichess game: that prompt
  used to read the saved slots, which a lobby game's pairing does not appear in,
  so a reset would have abandoned the game in progress for a local one without
  asking.

- **The lobby's chosen account did not post the seek**: The account picked in
  the Lichess Lobby was written to whichever Players slot was set to Lichess,
  and read back from that slot at seek time. A lobby Seek New Game with no slot
  set to Lichess therefore had nowhere to store the choice and nowhere to read
  it from, so it authenticated with the first saved credential -- signed in and
  listed in the lobby as one account, seeking as another. The account is now a
  property of the lobby (``game.lichess_account``) rather than of a player, so
  the same credential answers the lobby, the account picker and every seek no
  matter how the slots are configured. The per-player Account row is gone from
  the board's Players menu and from the web Players card, and the picker moved
  to the lobby on both. A configuration that bound an account to a slot adopts
  it as the lobby account on first load -- player 1 first, which is the slot the
  lobby signed in as when both were Lichess, and never from a slot that is no
  longer Lichess. Adoption only happens while the config file names no lobby
  account at all, so an upgrade keeps playing as the account it was bound to
  while a lobby account since set back to Default stays Default.

- **Rated could not be reached without a Lichess player slot**: Rated has always
  been stored once for the board (``game.lichess_rated``) but was drawn as a row
  on the player card, shown only while that slot was set to Lichess. Seek New
  Game posts a seek from a pairing the saved slots need not describe, so with
  neither slot online the toggle deciding whether that seek put the account's
  rating at stake was on no screen at all, and whatever it was last left at
  stood. Rated is now a lobby row directly under Account, on the board and on
  the web, beside the account whose rating it stakes. The board row reads Rated
  On or Rated Off with a checkbox, and selecting it writes the opposite value
  and redraws, so the state is visible before the seek goes out.

- **The rest of the board was English in every language**: localizing the
  Lichess screens left every other screen that does not come from the menu
  catalog still built from English literals, so a Spanish or French board read
  in its own language until it did anything: the countdown a long PLAY hold
  draws and the one an idle board shuts down on, the Shutting down, Rebooting,
  Suspending and Press [▶] screens, the startup steps from Bluetooth through
  Ready, the clock's White/Black and whose turn it is, CHECK and YOUR QUEEN, the
  move list's header and every paged screen's Page x of y and Next, the
  promotion pieces, the resign, draw and abort prompts including the one a
  lifted king raises, the take-back and new-game-from-here actions, End Game,
  the engine manager end to end (tier headings, an engine's status line, the
  install and uninstall screens, the stop, discard and paused-install prompts),
  the update screens, the Chromecast and Bluetooth screens with their pairing
  confirmation, the About readings, the WiFi panel, the coach's key and model
  rows, and the status and error text a Lichess game reports while connecting,
  seeking or waiting for an opponent. All of it now comes from the string
  bundle, translated into Spanish and French; roughly 130 strings in total.

  Two tests keep it that way. One walks the modules that draw the board and
  fails on any literal handed to a row, a widget or a splash, because a literal
  is invisible in English and invisible in review -- which is how all of the
  above survived the first pass; the strings that genuinely need no translation
  are listed with the reason rather than scattered. The other reads six of the
  screens in Spanish and checks the substitutions still land, since a scan can
  see that a string is looked up but not that it is the right one.

- **The Positions menu was English in every language**: opening Positions
  built every category and packaged position row by title-casing the INI
  key (Pawn Endgames, Mate In 1 Back Rank), so a Spanish or Dutch board
  showed an English menu. The chrome around it (End Game?, Cancel) already
  came from the string bundle. Categories and packaged names now come from
  that bundle on the board and the web; a custom overlay entry still
  title-cases because that name is the user's.

- **The board's Lichess screens were English in every language**: Players and
  its menus translate, but the lobby they open was built from English literals
  rather than from the catalog the web card reads, so choosing Español or
  Français gave a Spanish menu leading to an English lobby: its five rows, the
  help behind Rated, Ongoing Games and Challenges, the seek screen with its
  clock and colour, Connecting and Exiting, the accept prompts a challenge, a
  takeback or a draw offer interrupts a game with, the prompt after setting the
  pieces back to the start, and the errors for a missing scope, an expired
  token or an unreachable server. The rows and their help now come from the
  catalog nodes the web renders, which also ends the board's second copy of the
  Ongoing Games and Challenges text; the rest comes from the board string
  bundle, translated into both languages. Add Account and the delete
  confirmation followed, because the lobby's Account row is how they are
  reached. The Rated help the board had -- which says what a rated game costs
  and that it governs every seek, where the catalog only said Casual when off
  -- is now the one both surfaces show.

- **The Lichess lobby was part English on a Spanish or French board**: Rated
  arrived in the lobby without a Spanish or French entry, and a menu string a
  translation omits falls back to English with nothing raised or logged, so the
  row simply read Rated in both. The row that seeks was still called Nueva
  partida / Nouvelle partie, the name it carried before it was separated from
  the New Game that starts whichever players Settings describes, and its help
  still sent the reader to Players for the rated flag that now sits one row
  above it -- as did the English it was translated from. All three are
  corrected. Every translatable string in the menu catalog is now measured
  against each translation, so one added on the English side and forgotten on
  the other fails there instead of shipping as an English row in a Spanish
  menu; strings that read the same in both languages (Chess960, Bluetooth, each
  language named in itself) are listed as such with the reason, rather than
  going unnoticed among the gaps.

- **Lichess Lobby is the play menu on board and web**: User, Ongoing Games,
  Challenges, and New Game sat behind Players → Lichess Settings → Play. That
  extra Play page is gone. Players → Lichess Lobby opens those rows directly
  (Account first, Ongoing Games and Challenges always listed, then New Game).
  Selecting Ongoing or Challenges shows how that feature works, then the live
  list (or back to the lobby when the account has none). Account selects which
  saved Lichess login the board plays as. Add or delete logins is Accounts at
  the end of that picker. The web Players card uses the same
  catalog children and can start a join on the board.

- **Lichess Play no longer has an API Token row**: Accounts replaced that
  editor. The Play lobby still offered Token and wrote the active credential
  from there, a second place to change the same secret. Token is gone from
  Play; add or edit a login under Players → Lichess Lobby → Account →
  Accounts.

- **Start from position is refused when a slot is Lichess**: A Lichess seek,
  challenge, or ongoing game always starts from the opening. Loading a
  catalog position or Play-from-here with Lichess as a player would put this
  board on a different game than the remote opponent. Positions, the web
  setup-position API, and New-game-from-this-ply now show "Unavailable with
  lichess as a player" and leave the current game untouched.

- **Cancelling shutdown no longer blanks a waiting splash**: Long-press PLAY
  during a Lichess seek (or any other modal) replaced "Waiting for game" with
  the countdown splash, and the panel manager kept only one modal by stopping
  and discarding the first. Releasing PLAY removed the countdown and painted
  an empty stack -- game widgets are deferred so they will not wipe the wait
  splash, so the panel went white and slept while the seek kept running.
  Inactivity countdown used the same add/remove pair. A displaced modal is
  now parked without being stopped and put back when the one that covered it
  is removed. A real screen change still tears parked modals down, so a
  connected game cannot resurrect the wait splash.

- **Releasing PLAY during the shutdown countdown is honoured**: On a slow
  panel the countdown splash's first refresh blocks for up to two seconds,
  longer than the hold. The PLAY key-up was queued during that wait, then a
  drain of every pending key threw it away before the countdown looked for
  it, so the countdown ran to completion (or only cancelled on a second
  press). The drain is gone. PLAY is watched during the splash wait and
  through the countdown; other keys are still discarded so they cannot
  dispatch afterwards.

- **Localized e-paper headlines no longer run off the 128px panel**: French
  "Les blancs gagnent" is 135px at the game-over winner's 16px font, and
  Spanish "Ganan las blancas" is 129px, so centered drawing started at a
  negative x and lost glyphs on both edges. Turning wrap on in that 18px
  slot dropped the second line, so the user saw "Les blancs". TextWidget
  now has an overflow policy: wrap when the slot is tall enough for the
  wrapped lines, otherwise shrink the font until one line fits. Game-over
  gives the winner two lines when it needs them and merges the move count
  with the clocks if the extra line would push times off the 72px strip.
  Menu button labels, clock names, setup titles, and help headings use the
  same policy.

- **Original Centaur engine settings auto-save**: Engine and strength on
  Settings -> Original Centaur required an explicit Save, unlike every other
  value setting and unlike Direct Mode on the same card. Changing a dropdown
  and leaving the tab discarded the choice. The dropdowns now persist on
  change through the existing engine-proxy endpoint; changing the engine
  resets strength to Default the same way Players does. The Save button is
  gone. The card still says the proxy reads the values the next time Centaur
  launches.

- **Renaming, deleting or resetting an engine profile left the settings that
  used it pointing at nothing**: a profile is a section in the engine's `.uci`
  file, and three settings name one by section name -- both player slots'
  strength and the Original Centaur level. Nothing enforced the reference, and
  the failure was silent in the worst way: with no matching section the engine
  player falls back to the file's engine-wide `[DEFAULT]` at game start, so the
  board played at a strength nobody had chosen, with no error on the panel, in
  the web UI or in the log. "Reset profiles" dangled every reference to a custom
  profile by design, since it discards them. Renaming can no longer strand
  anything, because a profile's name is no longer its identity (see the profile
  identity entry under Added). A delete or a reset repoints only the references
  whose target is genuinely gone -- so a reset that re-derives the same ladder
  moves nothing -- and reports what it moved, making the consequence visible at
  the moment of the action instead of being discovered by playing a game at the
  wrong strength. The Original Centaur level's cached options are re-resolved
  with it, because the proxy launches from that cache rather than from the level.

- **A profile could be given a name that made it unmaintainable**: profile names
  were checked only for the four characters that break the INI file, but the name
  was also the REST path segment the editor addressed the profile through. `a/b`
  is a valid section header and does not match Flask's default string converter,
  so a profile named that way could be written and then never reached -- the
  editor listed it and offered Save and Delete buttons that could only ever fail,
  leaving hand-editing the file as the only way out. A name can no longer reach a
  section header at all, and the header itself is now checked against an allowed
  character set (letters in any language, digits, spaces and the punctuation real
  names use) rather than a list of four forbidden ones, which also refuses the
  INI comment delimiters and the interpolation character.

- **The Auto coach sized itself against the profile's name rather than its
  strength**: choosing Auto reads the opponent's rating to pick a coach, and it
  took that rating by finding the first run of digits in the stored strength
  selection. That selection is a profile name, so it worked only while the name
  happened to spell the Elo out: a profile named for its style or its net, or a
  seeded rung whose `UCI_Elo` had since been edited, gave a rating that was
  wrong or absent with nothing to show it. The rating is now read from the
  profile's own `UCI_Elo`, honouring `UCI_LimitStrength`, so an uncapped profile
  reports no rating rather than a made-up one, and a numeric selection still
  needs no lookup.

- **The board's strength picker kept offering profiles that had been edited
  away**: the on-device picker builds its rows from the engine's `.uci` and
  cached them for the life of the process, and nothing dropped that cache when a
  profile was written, renamed, deleted or reset from the web editor. The panel
  went on listing the pre-edit ladder until the app restarted, and picking a rung
  that no longer existed stored a strength that resolved to nothing. Every write
  to an engine's profiles now invalidates that engine's rows -- and only that
  engine's, since rebuilding another's can mean launching its binary.

- **An engine loaded with a strength that no longer existed said nothing about
  it**: a stored strength that matches no profile falls back to the engine-wide
  `[DEFAULT]`, which was the intended safety net but happened without a trace, so
  a slot that had lost its profile looked normal and played at full strength. The
  fallback now names the missing profile in the log, and a strength stored before
  profiles gained generated identities is resolved by its old name or by a
  profile's own name before the fallback is reached.

- **Saving an Elo profile asked whether to rename it, every time**: changing
  `UCI_Elo` on a rung named for a different Elo raised a confirmation dialog from
  inside the save, so adjusting the strength of `1000 ELO` could not be saved
  without answering a question about its name. Saving now asks nothing: a rung's
  Elo is stored once and its label is read back from that value, so there is no
  second copy in a name to drift out of step and nothing to reconcile.

- **Rodent IV's playing style was buried and unnamed**: Rodent picks among some
  thirty styles -- Defender, Fischer, Tal -- through a `Personality` option it
  offers once its personalities are installed, and a handshake against the
  installed binary shows that is the only strength axis it advertises (the
  opening-book options are suppressed, because it takes books from the style).
  Because the option is a fixed list rather than a file picker, it was sorted
  into Advanced alongside the evaluation tuning knobs, and it did not appear in
  profile labels at all -- so two profiles differing only in style read
  identically in every strength picker. The style now sits beside the Elo in the
  Strength card and leads the label, giving `Defender: 1700 ELO` for a capped
  profile and `Defender` for an uncapped one. An install whose personalities are
  missing advertises no such option, and its profiles are labelled by Elo as
  before.

- The CA install page's Windows download saved `UniversalChess-CA.pem`.
  Windows Certificate Manager associates `.crt` and `.cer`, not `.pem`, so
  double-clicking the file opened a text editor or the "how do you want to
  open this" dialog instead of the Certificate Import Wizard. Windows now
  gets the same DER `.crt` as Android (`/ca.pem?format=der`, filename
  `UniversalChess-CA.crt`).

- The System card's Bluetooth advertising row treated BCM43430B0 on kernel
  6.18 as a known fault regardless of BlueZ. That combination only breaks
  advertising when BlueZ still sends the over-long extended-advertising
  command (Raspberry Pi ``5.82-1.1+rpt1``). ``5.82-1.1+rpt2`` backports the
  upstream length fix, so stock advertising works and the install-time
  self-heal correctly leaves the distribution binary in place -- but the card
  kept a red "Known issue" badge. The row now requires a faulty BlueZ as well,
  and treats a patched ``bluetoothd`` as already repaired.

- The System card described none of an Orange Pi's radio. The chip was read out
  of the kernel log by matching a Broadcom part number, which no Allwinner
  kernel prints, so the chip row was blank -- and with no chip named, the
  Bluetooth advertising row had nothing to assess and reported "unknown" too.
  The board profile, which already holds this board's hardware differences, now
  declares the part the Orange Pi Zero 2W was brought up with (`UWE5622`, the
  reason the BlueZ self-heal is gated to Raspberry Pi), and the card falls back
  to it. The kernel log still wins where it speaks, because it names the
  Broadcom stepping the advertising verdict depends on and a profile can only
  name a part for the board as a whole. A part is declared only for the model it
  was observed on, so a board nobody has looked at still reports nothing rather
  than an inherited guess.

- The System card reported "BlueZ stack not determined" on every board the
  self-heal skips. Only the self-heal writes the marker the row reads, and it
  now declines to run on anything other than a Raspberry Pi, so an Orange Pi had
  no marker while plainly running the distribution's `bluetoothd`. The row now
  falls back to reading the disk: substituting a binary means diverting the
  stock one aside, so a `bluetoothd` with no diversion beside it is the stock
  stack. A diversion found without a marker still reports the patched stack and
  its warning, because the notice that a binary receives no distribution
  security updates must not depend on a state file that can be wiped. "Not
  determined" is now reserved for a host with no `bluetoothd` at all.

- The System card's Wi-Fi firmware row was blank on every board that is not a
  Raspberry Pi. It reported the version of one hardcoded package,
  `firmware-brcm80211`, which Armbian does not install: an Orange Pi Zero 2W
  loads `/lib/firmware/uwe5622/wcnmodem.bin` from `armbian-firmware`. The row now
  reports the first of the known firmware packages that dpkg says is installed,
  ordered so a board carrying both still names the one specific to its radio, and
  shows the package beside the version -- a version alone does not say what to
  upgrade. Installation is now judged by the package's dpkg status rather than by
  a stanza existing, because dpkg keeps the stanza and its version for a package
  removed without being purged, and lists packages it merely knows about. That
  board lists `firmware-brcm80211` without having it, so name-matching alone
  would have reported Broadcom firmware absent from the disk.

- Top-of-page status banners (a pending update, a running install, background)
  activity) sat in normal document flow above a sticky navbar, so they
  scrolled off as soon as the page left the top. They now stick with the
  navbar and stay visible while they are showing.

- Opening a web page while the board was unreachable showed a developer
  setup note (`vite.config.ts`, `run-react`) or the browser's "Failed to
  fetch", with no Retry. Those screens now explain that the board cannot
  be reached and offer Retry and Reload. Retry also runs when the navbar
  connection status returns to Connected (the same signal after a reboot
  or brief outage), so the error card and Connectivity/account load
  failures do not stay up until the user clicks.

- `deploy-to-pi.sh` could not copy onto a board whose SSH user has no
  passwordless sudo for rsync -- the stock state after the package install,
  whose tree is root-owned. The transfer ran `sudo rsync` as rsync's remote
  command, which has no TTY, so even a real terminal printed "a terminal is
  required to read the password" and aborted. When `sudo -n` is denied, the
  tree is now staged into that user's home and a single `ssh -t sudo rsync`
  installs it, so the password prompt can appear. Boards that already have
  passwordless sudo keep the direct elevated transfer.

- Settings claimed "You're running the latest version" when the update check
  could not reach GitHub -- no DNS, no route, the USB-gadget Client case with
  Internet Sharing not actually forwarding. A failed fetch used to look the
  same as "no newer release" (HTTP 200, `update_available: false`), so the
  confirmation was shown on a board that was behind. A failed check is now a
  503, the confirmation is withheld, and the page reports that the check
  failed. The board's Check for Updates splash reports the same failure
  instead of "Up to date".

- A board running a nightly build reported the Stable channel and followed it.
  The channel was stored in `update-state.json` as a preference defaulting to
  `stable`, and nothing ever compared it with the build that was installed, so a
  board flashed or sideloaded with a nightly read Stable in Settings and in the
  board's Updates menu, filtered every nightly release out of the check, and was
  offered the stable release -- which compares as newer, because a
  `nightly-<stamp>-<sha>` tag parses to no version numbers at all. Installing it
  declared a channel switch, so the board left the nightly channel without being
  asked. The channel is now read from the installed build, which names its
  channel both in the release tag written to `/opt/universalchess/VERSION` and in
  its dpkg version. The stored field now records only a switch the user has
  selected and not yet installed, and is dropped once the matching build is
  running: a selection still survives a restart while it is pending, and once it
  is carried out the board follows what it runs. Existing state files are
  reconciled once as they load -- a nightly recorded on a stable board can only
  have been chosen, so it is kept; any other combination defers to the installed
  build -- because applying that correction on every start would revert a nightly
  board's deliberate move to stable before it could be installed. Re-selecting
  the channel a board already runs also no longer discards a downloaded update
  that was waiting to be installed.

- Long-press OK on a highlighted move never opened Take back / New game from
  this position. The handler called `PlayerManager.supports_takeback` as a
  method; it is a bool property, so the events thread logged
  `'bool' object is not callable` and swallowed the key. The overlay now
  reads the property.

- Confirming Take back required pressing OK twice. A held OK still fires
  LONG_TICK at the 1s beep (the overlay appears while the button is down, so
  the hold is audible and visible). A latch meant to ignore that hold's
  release was never cleared when the wait loop consumed it, so the next
  short OK -- selecting the highlighted Take back row -- was discarded as a
  stale key-up. The matching release is ignored because the long-press
  already fired; unpaired leftover key-ups (bounce) are not taps; the next
  complete press is.

- New game from a highlighted move started from that ply's FEN with an empty
  history, and wrote no database row until a later move was played. It now
  copies the moves through that ply into a fresh recorded game (the same path
  as Play Game from here on the web). The original game stays in progress so
  Games can resume it. The source plies' evaluations are copied onto the new
  rows as well: resume resets the live analysis cache and restores the graph
  from `GameMove.eval_score`, so omitting them left the new game's graph empty.

- `tools/sd-card-setup/enable_usb_gadget.py` prepared a Shared-mode card while
  documenting a Client one. `rpi-usb-gadget on`, which the card's `runcmd`
  invokes, does not select either mode: it creates both NetworkManager profiles,
  activates `USB Gadget (shared)` with `connection.autoconnect yes`, leaves
  `USB Gadget (client)` at `no`, and enables `rpi-usb-gadget-ics.service` to move
  between the two according to whether the host appears to be offering Internet
  Sharing. A card prepared by the tool came up serving DHCP from
  `10.12.194.3-14` on `10.12.194.1`, with no route to the internet -- so the
  board could be reached but could not install anything, which is what preparing
  the card was for. The card now follows `on -f` with the commands that pin a
  mode: stop the watcher, set `connection.autoconnect` on both profiles, and
  activate the wanted one. Both profiles are named because setting only one
  leaves the other autoconnecting, and which NetworkManager then picks for `usb0`
  on the next boot is a race. Client is the default; `--shared` selects the other
  mode, and re-running with a different choice replaces the previous mode's
  commands rather than adding to them. `--auto` is the third option and pins
  nothing: it writes `systemctl enable --now rpi-usb-gadget-ics.service` and
  leaves the watcher to keep choosing, which is there to test the vendor
  behaviour against a pinned mode rather than to make a board more reliable --
  that watcher is what returned a Client preference as Shared after a reboot, and
  a switch to Shared can hand the host a route and DNS pointing at a Pi with no
  route out. The two flags are mutually exclusive, and the closing report
  describes the mode that was chosen: which addresses work, what the host must
  provide in Client, what it must not do in Shared, and that the mode is
  unsettled in Auto. `--shared` also skips the host DNS check, which waits for an
  interface a Shared-mode card never causes the host to create.

- A card prepared by `tools/sd-card-setup/enable_usb_gadget.py` was reachable
  over USB on its first boot and could stop being reachable on a later one.
  NetworkManager's own `85-nm-unmanaged.rules` marks every `DEVTYPE=="gadget"`
  interface unmanaged, and `rpi-usb-gadget` answers that with `nmcli device set
  usb0 managed yes` -- runtime state that does not survive a reboot. What carried
  a stock image across one was the generic `netplan-eth0` profile cloud-init
  generates, whose empty match happens to cover `usb0`; that is an accident of
  the image, and applying Client or Shared mode deletes that profile, because the
  same empty match otherwise claims the gadget as a DHCP client and fights
  Shared, where the Pi serves DHCP. With it gone `usb0` stayed at `STATE 10
  (unmanaged)`, `REASON 77 (unmanaged via udev rule)`: the cable enumerated and
  the link never got an address. The card now writes
  `/etc/NetworkManager/conf.d/90-uc-usb-gadget-managed.conf` through cloud-init
  `write_files`, claiming `usb0` regardless of what else exists, and the
  universal-chess package installs the identical file at the same path so a
  prepared card and an installed board are in the same state. The file is shown
  in full in the confirmation diff rather than elided like the DNS diagnostic.

- Leaving a position game dropped the user at the category list rather than at
  the position that had just been played. The Positions menu records the chosen
  category and position through a list the caller supplies, and both callers built
  that list fresh on every call, so the record was written into a value that was
  immediately discarded and the return path found nothing to return to. The board
  keeps one record for the life of the process, so the list reopens on the
  position played, and both entry points share it.

- Settings → Connectivity → Accounts showed "No accounts yet" to anyone who was
  not signed in, even when accounts were already saved on the board. The list
  endpoint requires authentication; a 401 used to be treated as a successful
  empty load, so the empty-state copy appeared and hid real accounts behind a
  confident claim. An unauthorized list no longer claims emptiness: the card
  offers Sign in (shared login dialog, then refetch) instead of forcing a login
  dialog on every anonymous page view, and "No accounts yet" is reserved for an
  authenticated empty list. Adding an account still prompts for login when
  needed. The same false-empty path hit Settings → Players: an online player's
  Account picker collapsed to "Default account" alone, with no way to sign in
  from that row. It now distinguishes unauthorized and failed reads from an
  empty store, offers Sign in or Retry, and only shows the Default/saved options
  after a successful authenticated load.

- The clock in the browser read minutes away from the board's own screen, but
  only for the player on move: a ten-minute clock showed five, then snapped back
  to the correct time the instant that player moved, with the error passing to
  the opponent. The board pushes a clock snapshot roughly once a second and the
  browser ages the running side between pushes so the countdown looks smooth.
  That aging subtracted the timestamp the board stamped on the snapshot from the
  browser's own clock -- two unsynchronised machines. The board is an RTC-less
  Pi, and over a USB link to a laptop it has no time source at all, so its clock
  sat several minutes off and the whole gap landed on whichever side was
  ticking. (A board running fast produced the opposite symptom: the active clock
  simply stopped moving.) The browser now measures the gap against its own
  monotonic timer, so the countdown is correct however far the board's clock has
  drifted. The board's screen was never affected -- it reads the clock directly
  and does no interpolation.

- Every load of the Settings page made the board fork a process and make a DBus
  round trip, just to report whether network time is switched on, and the board
  repeated it on every rebuild of its System menu. The endpoint that does this
  needs no authentication, so any client on the network could ask a Pi Zero to
  fork at will. Those flags change rarely, and almost always because the board
  itself changed them, so the reading is now held for five seconds and the
  repeated reads collapse to one. The clock reading itself is still taken live on
  every call -- it is the number the Device Clock card displays and the basis of
  the drift it reports, so holding it would show the same instant twice and
  understate the drift. Changing network time or setting the clock drops the held
  reading immediately, whether the change succeeded or not, since a failure can
  still have altered the state.

- The board showed nothing for the first two minutes after power-on, most of it
  spent waiting for something it does not use. The service was ordered after the
  network, which is only considered up once NetworkManager reports ready -- and
  on a Pi Zero that takes around fifty seconds, because NetworkManager rebuilds
  every stored network profile at each boot, including profiles for adapters the
  board does not have. Driving the screen and the chess board needs none of
  that: they are wired to the Pi directly. The service no longer waits, so the
  splash screen appears while the network is still coming up rather than after.
  Nothing about the network configuration itself is changed, so Wi-Fi boards are
  unaffected. The web interface still waits, since there is nobody to serve
  until the network exists.

- Loading the saved game settings took half a minute on a Pi Zero, stalling
  startup with the splash screen already showing. Reading one setting parsed the
  configuration file twice over and the packaged defaults once more, and a read
  of an absent key rewrote the live configuration to insert it -- so a read was
  not even a read. Settings are fetched a key at a time, which multiplied that
  cost by every key in a section. Reads no longer write, the packaged defaults
  are parsed once and kept, and a whole section is now read in a single parse.
  Loading the game settings went from 32.8 seconds to 0.7 seconds.

- The board spent five seconds of every startup rediscovering which display it
  has. A V1 panel wires its BUSY line the opposite way round, so the driver the
  board tries first can never succeed on one, and waits out its full timeout
  before falling back to the driver that does work. The answer was already being
  written to disk at the end of every startup and then ignored by the next one.
  Startup now tries the controller that last drove the panel first, which takes
  display initialization on a V1 board from 5.6 seconds to 0.5 seconds. The
  fallback is kept in both directions and needs no configuration, so swapping
  the panel -- or restoring a configuration taken from a different board --
  corrects itself on the next startup instead of leaving the screen blank.

- A board that was asleep when the app started was never found again, and the
  only cure was restarting the service. Discovery sends a wake pair and one
  address request, and every path that repeats them is reached only from a packet
  the board itself sent -- so a board that answers nothing was probed exactly
  once. Startup gave up after its three attempts and the app then polled address
  0x00/0x00 for the rest of the session, logging a request timeout every seven
  seconds, while the board sat awake beside it. Waking the board with its own
  power button after startup landed there, as did a board whose controller comes
  up slowly. Discovery now re-probes every ten seconds until the board answers,
  clearing any half-discovered address first so a retry does not depend on how
  far the previous attempt got, and stops as soon as discovery succeeds so a
  working board is never disturbed mid-session. The three startup attempts remain
  and still bound how long the splash waits, because recreating the controller
  reopens the serial port and a bare re-probe cannot; they no longer decide
  whether the board is ever found.

- BACK during a Lichess seek (or the connected splash before the first
  move) killed the app. `is_game_in_progress` is true only after a move, so
  BACK was treated as "no game" and `_return_to_menu` ran while
  `_start_game_mode` was still in `LichessPlayer.start()` (network auth on
  that thread). `protocol_manager` was already None when the start path
  called `set_on_promotion_needed`, which raised `AttributeError`; cleanup
  exited 0 and systemd left the unit inactive. BACK at ply 0 against a
  remote player now goes to the session handler (cancel seek, or the abort
  menu once the stream has accepted). If start is torn down mid-authenticate,
  `_start_game_mode` returns instead of touching the manager.

- Opening Challenges on the Lichess menu with a token that had `board:play`
  but not `challenge:read` showed a truncated HTTP 403 dump. Lichess returns
  403 `Missing scope: challenge:read` for `GET /api/challenge` (not 401);
  the panel only mapped 401 to a permission error. Challenges and Ongoing
  now name the missing scope, and the token-field help lists `board:play`,
  `challenge:read`, and `challenge:write`. Those errors (and the other Lichess
  failures: no token, start failed, unreachable server) were a one-row menu
  whose only entry was not selectable, so the copy was truncated and no key
  dismissed it. They are now a full-screen splash with the message and
  "Press any button"; any key (or 30s idle) returns to the menu.

- Starting a Lichess challenge (or New Game / Ongoing) from Players → Lichess
  Settings left the player-menu rows on the e-paper where the board should be;
  the analysis widget still painted below. The lobby started the game while
  those nested menus were still looping, returned `True` (not a break and not
  `START_GAME`), and the engine dropped the result so Players redrew over the
  board. The lobby now only stashes the join and returns `START_GAME`; nested
  menus unwind and Settings starts the game on a clear panel.

- Pressing New Game on the e-paper Lichess menu killed the app and left the
  last frame on the panel. A dedicated `start_lichess_game_service` imported
  `ProtocolManager` and `ControllerManager` from paths that do not exist; the
  `ModuleNotFoundError` was uncaught on the menu thread, so the main loop
  exited into cleanup with status 0 and systemd `Restart=on-failure` left the
  process dead. That launcher is gone: New Game uses the same PLAY path as
  Human vs Engine, a start failure is shown as an error instead of taking the
  process down, and an uncaught exception in the main loop now exits 1 so the
  unit restarts.

- Starting a Lichess game never showed a waiting splash (or "Connecting..." /
  "Loading Challenge..."). The dedicated Lichess menu added a splash with
  `add_widget` and did not wait for the e-paper, then constructed
  `DisplayManager`, whose first paint `clear_widgets` and draws the chess board
  -- wiping the splash before it appeared. PLAY with a Lichess player skipped
  the splash entirely. The waiting splash ("Waiting for game") is now shown
  through `show_fullscreen_splash` (which waits for the frame) and game widgets
  are deferred until the stream connects.

- The Lichess wait splash named only the path (Waiting / Connecting / Loading
  Challenge), so clock, rated, color, host:user, and rating range were hidden
  until an opponent appeared. The splash now lists those seek fields. Join of
  an ongoing game or challenge still omits the dummy 10+5 `LichessSeek` stores
  when the remote clock is already set. BACK during seek left the wait copy on
  screen while `stop_players` ran for several seconds, so the key looked
  ignored. The splash switches to "Exiting..." and waits for that frame before
  teardown.

- Setting the pieces back to the start during a Lichess game reset only the
  local board. The same `LichessPlayer` kept streaming the old remote game
  (`on_new_game` only logged), no new seek ran, and the waiting splash never
  appeared. A board-reset now leaves the remote game (abort, or resign if abort
  is no longer allowed) and rebuilds through `_start_game_mode`, which seeks a
  new opponent and shows "Waiting for game".

- **Setting up start after a Lichess seek connected cancelled the remote game**:
  The opponent can join while the pieces are still unset. Completing the
  starting setup then matched the live ply-0 position, but starting-position
  detection ran before that match and treated it as a board-reset new game:
  the remote game was abandoned and a local one started in its place.
  Physical start that already matches the live occupancy now continues that
  game (correction exits, clocks stay). Start while the live game has moved
  is still a new-game gesture.

- The screen went blank between the boot splash and the main menu, and again on
  entering a game, which looked like a fault rather than a transition. Every
  screen change clears the old contents and adds the new, but clearing also drew
  the status bar, so the transition sent the panel two images: one holding nothing
  but the 16-pixel status bar, then the real screen. Building the next screen
  takes long enough that the empty one was usually drawn first. Clearing now only
  discards the old contents without drawing, so a transition paints one image that
  already has the new screen in it.

- A game exported to PGN could not be replayed from the position it declared.
  Chess960 games and games started with "play from here" were exported from the
  standard opening regardless of the position they actually began from, because
  the exporter never read the stored start position or the variant. Adding moves
  to a game does not validate them, so the result was a well-formed file holding
  an impossible game. The board is now set up from the stored position and
  variant, and a move that is illegal in its position ends the export there
  rather than extending a corrupted game.

- `deploy-to-pi.sh` could report a completed deploy having transferred nothing.
  The install tree is deliberately root-owned (see Security), the transfer ran
  without elevation, and its output was piped through a filter that discarded
  the permission errors while forcing a successful exit status. The service was
  then restarted against unchanged code and reported healthy, so a board could
  run stale code for an entire debugging session. The transfer now runs with the
  elevation a root-owned tree requires, and a failure stops the deploy with the
  transfer's own exit status rather than restarting anything. Ownership is no
  longer taken from the sending machine either: the previous flags told a root
  receiver to stamp the developer's own numeric user and group onto every file,
  which silently undid the root ownership the passwordless-sudo grants rely on.
  The runtime data directories are handed back to the service account after each
  transfer, matching what installing the package does.

- The web interface could die as it started and come back only because systemd
  restarted it, leaving the page unreachable in the meantime. The board rewrites
  the e-paper screenshot in place on every panel refresh, and the web process
  decoded that file while loading, so a start that landed mid-rewrite raised on a
  half-written image and killed the process before it began serving. Loading
  takes around 70 seconds on the board's single core, so the window was a wide
  one. The decode was redundant as well as fragile: the only feature that uses
  the cached screenshot -- the Chromecast layout that shows the board's screen
  beside the position -- already reloads it whenever the file changes and already
  copes with a screenshot it cannot read. Reading it at startup added a way to
  fail and nothing else.

- `deploy-to-pi.sh` could report a completed deploy while the board was
  crash-looping on the code it had just shipped; the startup crash above reached
  a board that way. Its check waited three seconds and asked systemd whether the
  services were running, which on this hardware happened well before the web
  interface had finished loading, and because both services are configured to
  restart automatically, one that crashes reports itself running again moments
  later -- a crash loop was indistinguishable from health. The deploy now waits
  for the web interface to actually answer a request, for up to four minutes, and
  fails if either service restarts while it waits, so a reported success means
  the shipped code loaded and served. A failure reports the service's own log
  next to the journal, because the application writes to a log file rather than
  the journal, which is why the previous check's search of the journal found
  nothing to report. Its exit status distinguishes a service that never came up
  from one that came up and died.

- An engine player could stop moving for the rest of the session after a restart
  on a busy board, leaving the board waiting for a move that never came. Two
  faults combined. Where two consumers want the same engine, the second waits for
  the first one's load instead of starting a duplicate, but that wait gave up
  after 60 seconds and could not tell a load still running from one that had
  failed -- on a single-core board still finishing an update, loading Stockfish
  took 67 seconds, so it reported a failure that never happened. The waiter now
  distinguishes the two, and waits long enough to cover a load that slow. The
  player then treated the reported failure as final, so an engine that finished
  loading moments later was never picked up; a failed load is now retried, up to
  a bounded number of attempts, when a move is next requested.

- After updating, a board could come back with the chess board undetected and
  the main service restarting every thirteen seconds. Making the install tree
  root-owned (see Security) collided with the service still running from that
  directory: lgpio creates a notification FIFO in the process working directory
  the moment `RPi.GPIO` is imported, and the account the board runs as can no
  longer write there. The import failed, so the process died before it ever
  opened the serial port -- the board was not detected because nothing was left
  running to look for it. The service now works from `/run/universalchess`,
  which is created for that account on every boot, so no stale FIFO survives a
  restart either. Widening the tree's permissions would have undone the
  privilege boundary, so the working directory is what moved.
- The two service logs were overwritten from the beginning on every restart
  instead of being appended to, which left each file carrying a fresh timestamp
  over hours-old contents. Reading the end of a log showed history rather than
  what had just happened, which is exactly backwards during an incident. Both
  are now appended to and rotated, so they stay readable without growing without
  bound on the SD card.
- Powering the Pi off killed a running engine install outright, leaving a
  part-written build tree that came back only as an "interrupted" install. A
  source build can hold the better part of an hour, and the idle timeout can fire
  in the middle of one. Shutdown and reboot now ask the install to stop first and
  wait for it to wind down, so it records a real resume point and is picked up
  where it left off. The wait watches the persisted state rather than the reply
  to the request, because an accepted stop only sets the cancel flag -- the build
  still has to reach a command boundary and write the resume point. It is bounded
  at twenty seconds and the power-off proceeds regardless: a shutdown that hangs
  on a build that will not unwind is worse than one that gives up, and startup
  reconciliation still recovers what was reached. The board asks the web, which
  owns installs, over the socket that already connects them; the web stops the
  install itself only when the board is not running to do it.
- The shutdown hook that exists to sleep the Centaur controller when the app is
  not running had never once worked, so a board whose app had crashed or been
  stopped kept its controller powered after the Pi shut down and drained the
  controller's own battery. `board.controller` is only ever assigned by
  `init_board`, which runs in the main service; the hook runs in its own process,
  so the global was `None` there and every attempt failed on
  `'NoneType' object has no attribute 'sleep'` -- twenty-six consecutive
  shutdowns on the development board, none successful. The hook now initialises a
  controller for itself, bounded so an absent or sleeping board cannot hold
  shutdown open, and its unit carries an explicit `TimeoutStartSec` instead of
  inheriting `infinity`. The two sleepers also stopped working against each
  other: the main service tried to disarm the hook with `systemctl stop`, which
  does nothing to an inactive `oneshot` that `shutdown.target` then pulls in
  anyway, so on a clean shutdown the hook still ran and logged "battery may
  drain" about a controller the main service had already slept correctly.
  Whichever process sleeps the controller now records it under
  `/run/universalchess`, which is emptied every boot, and the hook exits
  immediately when it finds that record -- so the warning now appears only when a
  controller really was left powered. The hook also defers while the main service
  is still running, because a hook that can now open a controller of its own must
  not open a second connection to the serial port the running service holds; a
  service state it cannot read counts as not running, since refusing to sleep is
  the one outcome this hook exists to prevent. Making the hook work exposed what
  it was wired to: it was pulled in by `shutdown.target`, which is reached by
  reboot, kexec and soft-reboot as well as by power-off. The controller is the
  board's power manager and answers the sleep command by cutting power to the Pi,
  which is why the app itself sleeps it only when shutting down and never when
  rebooting -- so a working hook on that wiring would have turned every reboot,
  including Reboot from the menu and the web, into a power-off that left the board
  dark until someone pressed the power button. The unit is now wanted by
  `poweroff.target` and `halt.target` alone, and installation removes the
  `shutdown.target` link that earlier releases created, since enabling a unit
  never retires symlinks an older `[Install]` section left behind. A power-off
  on a real board then showed the hook still leaving the controller awake, for a
  second reason: a start job carries no ordering against a concurrent stop job,
  so the hook ran while the main service was still `deactivating` with its main
  PID alive and holding the serial port. It therefore stood down in favour of a
  service that had been stopped by systemd rather than by a menu shutdown and so
  would never sleep the controller. The unit is now ordered
  `After=universal-chess.service`, which a probe pair measured mid-shutdown as
  the difference between finding that service `deactivating` and finding it
  `inactive` with no process left; standing down is logged as a warning naming
  the consequence, because it now describes a controller nobody will sleep.
  What let the original defect hide for so long was that the hook's only record
  of it went to the journal, which Raspberry Pi OS keeps in RAM
  (`Storage=volatile`), so each failure was erased by the boot that followed it.
  The hook now files its outcome in the Event Log under Settings, which lives in
  /var/lib and survives: a fallback sleep it performed, a controller it could not
  get an acknowledgement from, and a shutdown it stood down for. The ordinary
  power-off, where the app slept the controller itself, still records nothing --
  that path is every normal shutdown and would bury the log in routine lines.
- Reboot and Shutdown did nothing on a board whose service user has no blanket
  passwordless sudo. Both actions -- from the Power menu and from the web -- end
  in `platform/system_power.py` running `sudo systemctl reboot` or
  `sudo systemctl poweroff`, and the package granted neither. It wires
  passwordless sudo for every other privileged action (chpasswd, bt-admin, the
  updater, the clock, the USB gadget helper) and for exactly one systemctl form,
  `restart universal-chess.service`, so the power commands fell through to a
  password prompt with no TTY behind them and were denied. The Raspberry Pi
  stayed up while the app completed its own cleanup and exited, which looked
  like the menu had merely killed the board software. Installation now writes
  `/etc/sudoers.d/universal-chess-power` granting those two commands, each
  pinned with its verb -- a bare `systemctl` grant would be root over every unit
  -- and validated with `visudo` like the other drop-ins. It is a separate file
  from the restart grant, which is written truncating and would otherwise erase
  it. Boards where an operator had added a blanket NOPASSWD rule by hand never
  saw the defect, which is why it survived to now.
- Wi-Fi was wholly non-functional on such a board, for the same reason and
  invisibly. Every Wi-Fi action needs root -- the scan runs `iwlist`, connect and
  forget run `nmcli`, the radio switch runs `rfkill` -- and none of them was
  granted, so each was denied. Because no caller read the exit status, all of it
  was reported as success: the network list came back empty as though no access
  point were in range, connecting appeared to work and changed nothing, and the
  radio toggle moved in the UI while the radio never moved. Those actions now go
  through one pinned helper, `scripts/uc-wifi-admin`, which the package grants
  passwordless sudo on and which performs only those operations; granting `nmcli`
  itself would be control over every connection on the board, and `rfkill` over
  every radio. The helper's exit status now reaches the caller, so a real failure
  is reported as one. It also takes the passphrase on stdin and passes it to
  NetworkManager through a 0600 file that is removed before it returns, where the
  WPA2 fallback path previously put the passphrase on a command line any local
  user could read from `ps`.
- `scripts/check-updates.sh --install` no longer prints a permission error in the
  middle of a successful install. apt drops privileges to its `_apt` user for the
  acquire step even when the "download" is copying a local file into place, and
  the `mktemp -d` staging directory is 0700, so `_apt` could not traverse it: apt
  reported `couldn't be accessed by user '_apt' ... (13: Permission denied)` and
  redid the copy as root. Nothing was broken, which is the problem -- it read as a
  failure partway through an operation that had worked, on every update. The
  staging directory is now made traversable so apt keeps its own sandbox rather
  than escalating. Only the mode changes; the directory stays owned by the
  invoking user, so no other local user can substitute the package between the
  download and the install.
- Bluetooth startup no longer attempts `sudo service rfcomm stop`. The legacy
  `rfcomm.service` it aimed at is disabled and stopped by the package install, so
  there was nothing left for a runtime stop to do, and the call was never granted
  -- on a board without a blanket passwordless rule it only cost a failed sudo
  authentication and the pause that went with it, on every startup, before the
  RFCOMM channel could be bound. What actually holds the channel is a stray
  `rfcomm` process, and the sweep that clears those is unchanged.
- Every privileged command the product runs is now checked against the grants the
  package installs, by a test that parses both sides -- the `sudo` invocations out
  of the application's syntax trees and the `NOPASSWD` rules out of the postinst.
  Nothing connected the two before, which is how the power and Wi-Fi defects
  above reached a release: the grants live in a shell script and the calls live in
  argv lists spread across the package, and reviewing that by eye does not work
  (the first audit written by hand missed the `rfkill` calls for using single
  quotes and the `os.system` calls for not being argv lists). Call sites that are
  deliberately ungranted are listed with their reasons, and a listed site that has
  gone away fails the test too, so the list cannot become cover for a call that
  comes back. The commands that were already granted now run under `sudo -n`, so
  a missing grant fails immediately instead of waiting on a password prompt no
  service can answer.
- A board that could not read its battery powered itself off after fifteen idle
  minutes, ending any engine install that was running. The idle power-off exists
  to save the battery, and the power source is known only from the baseboard's
  five-second `DGT_SEND_BATTERY_INFO` poll: when that request times out -- no
  baseboard attached, or a dead serial link -- the poll records nothing, and the
  charger flag it never wrote was a plain `False`, which reads as "on battery".
  A mains-fed Raspberry Pi with no battery at all was therefore given a
  battery-saving shutdown. The power source is now a three-way state, so a board
  that has never reported one does not power off, for the same reason
  `WIFI_ABSENT` is not `WIFI_DISABLED`: not having been told is not a reading.
  Silence *after* a reading keeps the last known source, because a board that
  reported "on battery" and then lost its link may really be on a battery and
  must still power off. The charger flag the status bar and web payload read is
  unchanged, so no indicator behaves differently.
- After a board power cycle the Safari PWA could stay on an empty battery glyph
  (and a stalled clock) until several manual reloads. The live `/events` stream
  only seeded game state on connect, wake only forced a new EventSource when the
  prior one was already `CLOSED` (Safari often left it stuck in `CONNECTING`),
  and the battery indicator's one-shot mount fetch often landed before the board
  had a reading. Connect now also seeds or pulls battery and clock snapshots, a
  foreground/`online` wake always opens a fresh stream, and the battery indicator
  re-fetches while connected and still unknown.
- Settings → System → Power left "Shutting down. The web interface is now
  unavailable." (and the matching reboot copy) on screen after the board was
  back and the navbar already read Connected. Shutdown and reboot return success
  before the drop, and the SPA stays on that page through the outage, so the
  banner was still claiming the UI was gone. A successful Power outcome now
  clears when connection status returns to Connected after having left it; a
  failed action is left in place.
- Changing a player's engine (or other player-defining setting) from the web did
  not take effect in the next game: a board-reset new game restarts play in place
  and reused the player objects built when the game first started, so the old
  engine kept playing. The running game's player configuration is now captured as
  a signature; when it differs from the current settings, the next new game (board
  reset or menu PLAY) rebuilds the players so the new engine is loaded.
- Web UI engine install never started and the progress notice spun forever: the
  React Settings page posted to `/api/engines/{install,uninstall}/<name>` with no
  body, but the backend expects `POST /api/engines/{install,uninstall}` with the
  engine name in the JSON body, so every request 404'd. The page now uses the
  correct contract, polls `/api/engines/status`, surfaces failures, shows
  `Installing <name>...` on the button with the "may take several minutes" notice
  beside it, and restores the in-progress state after a page reload
- Source-build progress reported far too much done and far too little time left:
  a Reckless install showed 94% and "less than a minute remaining" at its 17th
  module of ~120, with most of an hour still to run. Three causes, each fixed:
  the completed fraction divided by the units already sampled rather than by all
  of them, and since every sampled unit but the one compiling is finished, that
  ratio was near 1 from the third unit onwards; a Rust build's units were counted
  as `.rs` files when `rustc` compiles a whole crate per invocation, inflating a
  36-crate build to ~120 units and naming every crate `lib.rs`; and the compile
  rate was measured from the start of the build command, charging Reckless's
  rustup bootstrap and cargo registry fetch to every unit still to come. The
  remaining time is also withdrawn, rather than repeated, once the module in
  flight has run longer than the whole projection allows -- the case of a final
  fat-LTO crate that outlasts the 35 before it
- Engine timeout issues on Raspberry Pi (removed default timeouts)
- Multiple engine instance conflicts (via EngineRegistry)
- dpkg lock conflicts during installation
- Various build script path issues

### Security

- **The install directory is no longer writable by the service user**: everything
  under `/opt/universalchess` was owned by the account the board and web app run
  as. That included `scripts/`, whose helpers each have a passwordless sudo
  grant -- so anything able to write a file as that user could replace a helper
  and have root run it. Code, scripts and the virtualenv are now root-owned, and
  only the directories the product genuinely writes (`config`, `db`, `engines`,
  `tmp`, `web/static`, `pending-updates`) belong to the service user. The upgrade
  resets ownership, so boards that were already exposed are corrected rather than
  left as they were.
  - TLS material stays root-owned, so the certificate authority and server
    private keys can no longer be read or replaced by the web process. The
    certificate download and iOS profile still work, as those read only the
    public certificate.
  - Python bytecode is now compiled during installation. It was previously
    written on first import, which a root-owned tree no longer permits, and
    without precompilation every startup would re-parse every module.
- **Updates must be signed to install**: the updater downloads into a directory
  the service user can write, and the root helper installs whatever is there, so
  a checksum from that same directory proved nothing. Releases now carry a
  detached signature over `SHA256SUMS.txt`, and the root helper verifies it
  against a keyring shipped inside the package before the package is allowed to
  run any code. A missing keyring, manifest or signature refuses the install
  rather than skipping the check.
  - The helper also refuses a version older than the one installed, so a genuine
    but outdated release cannot be used to reintroduce a fixed issue. Deliberate
    stable/nightly switches remain possible.
  - The build refuses to produce a package without the signing keyring, since
    such a package would install and then be unable to verify any later update.
- **Downloads are verified before they are staged**: `.deb` downloads are checked
  against the release's published checksums and discarded on any mismatch,
  missing entry or unfetchable manifest. Previously the file was downloaded and
  installed as root with no integrity check at all.
- **Python dependencies now ship inside the package**: installing fetched them
  from PyPI and ran them as root, so the code a board ended up with was whatever
  the index served that day -- the package was verified end to end and then
  pulled unverified code into itself. The wheels now travel inside the signed
  `.deb`, pinned to exact versions and hashes, and the install no longer contacts
  an index at all. Installs are faster on the slowest hardware as a result: the
  chess library had no published wheel, so every board downloaded six megabytes
  of source and compiled it.
  - Three dependencies nothing imports were removed, one of which was compiled
    from source on every Pi Zero install for a library the product never calls.
  - Pinning those versions also froze them, so security fixes no longer arrive by
    themselves. Vulnerability alerting now covers the pinned set, and a workflow
    re-resolves the whole closure on a schedule or on demand, verifying offline
    that the result still installs before proposing it.
    - That refresh has now delivered its first update, moving `webencodings` from
      0.5.1 to 0.6.1. The package had been silent since April 2017, and a
      long-dormant dependency suddenly publishing is also what a hijacked one
      looks like, so the artifact's publisher attestation was checked against the
      upstream project before the update was accepted, not just its hash against
      the index. The two consumers in the closure use four functions between them,
      all still present; the modules the release deletes are imported by nothing
      the package ships.
    - Later refreshes are not recorded here individually. Which version of a
      vendored library a board runs is not something a board can show, so the
      generated commit names the pins it moved and declares that no entry is
      owed, which keeps the omission visible to the changelog audit rather than
      silent. A refresh that resolves an advisory is owed an entry and gets one;
      the generator cannot tell that case from routine drift, so the judgement is
      made when the pull request is reviewed.
- **The Lichess API token is no longer disclosed**: the settings endpoint
  returned it in clear text without authentication. It is now redacted, and
  saving settings without it leaves the stored token unchanged rather than
  erasing it.
- **Web coach endpoints no longer generate on request**: unauthenticated callers
  could trigger model calls. The statement endpoint now only returns what the
  board already produced, the tip endpoint is removed, and the model list
  requires authentication.
- **Engine option probing is cached**: an unauthenticated request could start an
  engine process per call. Results are cached per engine binary and shared
  between concurrent callers, and the cache is cleared when an engine is
  installed.
- **Removed the WebDAV path that made arbitrary files world-writable**: a request
  could set mode 0777 on any path listed in a file at the share root.
- **Password handling hardened**: WebDAV password hashes are compared in constant
  time, and the minimum password length is raised from 4 to 6.
- **Supply chain**: all GitHub Actions are pinned to immutable commit SHAs rather
  than mutable tags, and a high-severity `js-yaml` advisory reached through a
  development dependency is resolved.

### Notes

- Minimum Python version: 3.9 (Debian Bullseye)
- Maximum tested Python version: 3.13 (Debian Trixie)
- Requires Raspberry Pi with DGT Centaur board

---

## [1.3.3] - Previous Release (DGTCentaurMods)

See the [DGTCentaurMods repository](https://github.com/EdNekebno/DGTCentaurMods)
for historical release notes.


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

### Changed

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
  - Positions moves up beside Game. It is a board Settings entry that the web
    renders as its own page, so no tab order governed where it sat.

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

### Removed

- **Deprecated Engines**: Fire, Laser (x86-only, incompatible with ARM)
- **Legacy CI**: Docker-based cron CI system (moved to `.github/legacy-ci/`)
- **Obsolete Tests**: Removed outdated promotion hardware tests

### Fixed

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


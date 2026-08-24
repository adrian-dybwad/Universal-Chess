"""Tests for the shipped package's ``Depends`` field.

Engine build tools are deliberately NOT declared here. They are provisioned on
demand by the pinned ``uc-engine-deps`` helper, which holds the only sudoers
grant that authorizes apt, so declaring them again in ``Depends`` would put them
on every board including the majority that never build an engine.

This file previously pinned ``git`` and ``build-essential`` into ``Depends`` as
the fix for the CT800 field failure ("Could not install required build
dependencies: git"). That guarantee now lives with the mechanism that provides
it, in ``test_engine_deps_helper.py``: the helper's allow-list must cover every
package the engine catalog can request, the postinst must grant the helper
passwordless sudo, and the installer must invoke it with ``sudo -n``. Those three
together are what make a build dependency obtainable, so asserting a duplicate
copy of the list here added no protection.

The compiler is the one exception, and it is not an engine dependency at all:
the Centaur display shim is compiled on-device outside the engine installer, so
the helper never runs for it. That is what the compiler test below pins.
"""

import re
from pathlib import Path

import universalchess.managers.engine_manager as engine_manager_module

# Repo layout: .../src/universalchess/managers/engine_manager.py
# -> repo root is four parents up, then packaging/deb-root/DEBIAN/control.
CONTROL = (
    Path(engine_manager_module.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "control"
)


# Packages that provide a native C compiler. `build-essential` pulls gcc plus
# the libc headers the shim link needs; a bare `gcc` plus `libc6-dev` would also
# satisfy it, so either spelling passes.
NATIVE_COMPILER_PROVIDERS = frozenset({"build-essential", "gcc"})


def _declared_depends() -> set:
    """Package names in the control file's ``Depends`` field."""
    return set(_depends_occurrences())


def _depends_occurrences() -> list:
    """Package names in ``Depends`` in order, keeping duplicates.

    Parses the Debian field properly rather than substring-matching the raw text:
    a naive ``"git" in text`` search matches ``gir1.2-glib-2.0``. Handles folded
    continuation lines, ``|`` alternatives, and parenthesised version constraints.
    """
    text = CONTROL.read_text()
    match = re.search(r"^Depends:(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no Depends field in {CONTROL}"

    names = []
    for clause in match.group(1).split(","):
        for alternative in clause.split("|"):
            name = alternative.split("(")[0].strip()
            if name:
                names.append(name)
    return names


def test_depends_provides_an_openpgp_verifier_on_both_debian_releases():
    """``Depends`` must offer both ``sqv`` and ``gpgv`` as alternatives.

    Why this test exists: the install helper verifies each update's signed
    manifest, and which verifier is guaranteed present depends on the Debian
    release. On bookworm ``apt`` depends on ``gpgv``; on trixie ``apt`` is built
    against Sequoia and depends on ``sqv (>= 1.3.0)`` instead, so ``gpgv`` may be
    absent there. Declaring them as alternatives means the dependency is already
    satisfied on both -- nothing is downloaded, and an update still installs on a
    board with no network.

    How a regression manifests: narrowing this to ``gpgv`` alone makes every
    trixie update pull an extra package mid-install, and fail outright when the
    board is offline or the archive is unreachable.
    """
    declared = _declared_depends()

    assert {"sqv", "gpgv"} <= declared, (
        "Depends must list both sqv and gpgv as alternatives so the signature "
        f"verifier is already installed on bookworm and trixie alike; got {declared}"
    )


def test_depends_provides_a_native_compiler_for_the_display_shim():
    """``Depends`` must provide a native C compiler.

    Why this test exists: this is the one compile that is NOT an engine build, so
    it cannot come from the uc-engine-deps helper. ``centaur_display.shim_builder``
    compiles ``spishim.so`` on-device for Centaur translate mode, and on a 32-bit
    board ``_resolve_compiler`` returns the native ``gcc``. Nothing else installs
    it there: ``centaur-armhf-setup`` provisions the *cross* toolchain and exits
    immediately when the host arch is not arm64, and ``Recommends`` covers only
    the cross packages. So on a Pi Zero W or Pi 1 the compiler must come from
    this field.

    How a regression manifests: removing build-essential (e.g. while trimming
    engine build tools now that the helper supplies them) leaves an armhf board
    with no compiler, and Centaur translate mode fails at first use with
    "compiler 'gcc' not found" -- on a board that has no other way to obtain it.
    It would still pass on any board whose base image happens to ship gcc, which
    is precisely the ambient-state assumption this pins down.
    """
    declared = _declared_depends()
    assert declared & NATIVE_COMPILER_PROVIDERS, (
        "Depends provides no native C compiler; the Centaur display shim cannot "
        f"be built on a 32-bit board. Expected one of: {sorted(NATIVE_COMPILER_PROVIDERS)}"
    )


def test_depends_provides_fuser_for_the_dpkg_lock_wait():
    """``Depends`` must guarantee ``fuser`` (psmisc) is present.

    Why this test exists: the postinst's fresh-install reboot waits for the
    dpkg locks to clear by polling ``fuser``, because rebooting during the
    transaction (trigger processing continues after this postinst returns) can
    leave packages half-configured. Without fuser the reboot command cannot
    tell whether the transaction ended, so it declines to reboot at all -- a
    fresh install that never comes back up on its own. psmisc is present on
    stock Raspberry Pi OS, which is exactly why relying on it silently is a
    trap: the dependency is real but invisible until a slimmer image appears.

    How a regression manifests: dropping psmisc leaves the wait unable to run,
    and the failure is a fresh install that prints "rebooting to complete
    setup" and then does not.
    """
    assert "psmisc" in _declared_depends(), (
        "Depends must include psmisc; the postinst reboot wait needs fuser to "
        "detect that the dpkg transaction has finished"
    )


def test_depends_provides_logrotate_for_the_service_logs():
    """``Depends`` must guarantee ``logrotate`` is present.

    Why this test exists: the two service units append their output to files in
    /var/log rather than the journal, so nothing bounds those files but the
    logrotate config the package ships. Raspberry Pi OS installs logrotate, which
    is exactly why relying on it silently is a trap -- the config is inert on any
    image that does not, and the failure is a board that fills its SD card.

    How a regression manifests: no error at install or at runtime. Months later
    a long-running board runs out of space in /var and fails at whatever writes
    next, with nothing pointing back at logging.
    """
    assert "logrotate" in _declared_depends(), (
        "Depends must include logrotate; the service units append to /var/log and "
        "the shipped rotation config is the only thing bounding those files"
    )


def test_depends_provides_ensurepip_for_the_application_venv():
    """``Depends`` must guarantee ``python3-venv`` is present.

    Why this test exists: the postinst builds the application environment with
    ``python3 -m venv --system-site-packages`` and then installs the vendored
    wheels with that venv's pip. Debian splits ``ensurepip`` -- and the bundled
    pip wheel it seeds the venv from -- out of the interpreter into
    ``python3-venv``. Raspberry Pi OS ships it in the base image, which is
    exactly why relying on it silently is a trap: on an Armbian trixie image it
    is absent, and the venv step aborts with "ensurepip is not available ...
    install the python3-venv package".

    ``python3-venv`` rather than a versioned ``python3.13-venv`` because the
    unversioned metapackage tracks whichever interpreter the release makes
    default, so the field stays correct across bookworm and trixie.

    How a regression manifests: the install fails at "::: Installing python
    packages" with a non-zero postinst exit, leaving universal-chess
    half-configured -- no venv, so the service cannot start on the next boot
    either.
    """
    assert "python3-venv" in _declared_depends(), (
        "Depends must include python3-venv; without it `python3 -m venv` has no "
        "ensurepip and the postinst cannot build /opt/universalchess/.venv"
    )


def test_depends_omits_the_incompatible_debian_pam_binding():
    """``Depends`` must not pull in ``python3-pam``.

    Why this test exists: Debian's ``python3-pam`` is the PyPAM C extension, which
    installs a module named ``PAM`` and has an API unrelated to the one this code
    calls. The web UI authenticates through ``pam.pam().authenticate(...)``, which
    is the PyPI ``python-pam`` distribution installed into the venv, and nothing
    in the tree imports ``PAM`` at all. Declaring it therefore installs an unused
    C extension on every board -- and names, in the dependency list, the very
    package the requirements file warns must not be confused for the real one.

    How a regression manifests: no immediate failure, which is the hazard. The
    risk it leaves behind is that a system-visible distribution claiming the same
    name would let pip consider the requirement already satisfied and skip the
    module that actually works, turning every login into an authentication
    failure that looks like a wrong password.
    """
    assert "python3-pam" not in _declared_depends(), (
        "Depends must not include python3-pam: it provides the incompatible "
        "PyPAM `PAM` module, which nothing imports. Web auth uses the PyPI "
        "python-pam distribution (module `pam`) installed into the venv."
    )


def test_depends_lists_each_package_once():
    """No package may appear twice in ``Depends``.

    Why this test exists: a duplicated entry is a merge artifact that hides real
    edits -- the field is one long line, so a second copy of a name makes it
    ambiguous whether a dependency was added deliberately or pasted twice, and
    it defeats review of exactly this kind of change. It caught a duplicate
    ``python3-github`` when it was written.

    How a regression manifests: appending a package already present leaves the
    build working (dpkg tolerates duplicates) while the field silently accretes
    noise; this assertion names the repeated package.
    """
    occurrences = _depends_occurrences()
    duplicates = sorted({name for name in occurrences if occurrences.count(name) > 1})
    assert not duplicates, f"Depends lists these packages more than once: {duplicates}"


def test_rpi_gpio_is_an_alternative_to_libgpiod_not_a_hard_depends():
    """``python3-rpi.gpio`` must be optional when ``python3-libgpiod`` is present.

    Why this test exists: ``python3-rpi.gpio`` is a Raspberry Pi OS package that
    talks BCM numbers through the Pi GPIO character device. Armbian on an
    Orange Pi Zero 2W either does not ship it or ships a stub that cannot drive
    the H618. A hard Depends makes dpkg refuse (or force-install the wrong
    GPIO stack) on that board. ``python3-libgpiod`` is the backend the Orange
    Pi profile uses. Listing them as alternatives keeps Pi images on rpi.gpio
    (first alternative, already installed) and lets Armbian satisfy the field
    with libgpiod.

    How a regression manifests: a hard ``python3-rpi.gpio`` Depends fails the
    Universal Chess .deb install on Armbian, or pulls a Pi-only GPIO library
    onto the H618 and the e-paper driver claims the wrong lines.
    """
    text = CONTROL.read_text()
    match = re.search(r"^Depends:(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no Depends field in {CONTROL}"
    gpio_clause = None
    for clause in match.group(1).split(","):
        if "python3-rpi.gpio" in clause:
            gpio_clause = clause
            break
    assert gpio_clause is not None, "Depends no longer mentions python3-rpi.gpio"
    assert "|" in gpio_clause, (
        "python3-rpi.gpio must be an alternative (|) so Armbian can satisfy "
        f"Depends with python3-libgpiod; got {gpio_clause!r}"
    )
    alternatives = {
        alternative.split("(")[0].strip() for alternative in gpio_clause.split("|")
    }
    assert "python3-rpi.gpio" in alternatives
    assert "python3-libgpiod" in alternatives, (
        "the rpi.gpio alternative must include python3-libgpiod for Orange Pi; "
        f"got {sorted(alternatives)}"
    )


def test_libgpiod_is_the_first_gpio_alternative():
    """``python3-libgpiod`` must be listed before ``python3-rpi.gpio``.

    Why this test exists: order decides which package actually gets installed.
    apt satisfies an alternative that is already installed, and otherwise takes
    the first one -- so with rpi.gpio first, an Armbian board (where neither is
    present) installed rpi.gpio and never got libgpiod. The e-paper backend for
    that board imports ``gpiod``, so the panel failed at startup with "No module
    named 'gpiod'". The clause was written believing Armbian could not install
    rpi.gpio at all; Debian trixie carries it for arm64, so the alternative
    silently resolved the wrong way on the one board it existed for.

    Putting libgpiod first inverts that: Armbian installs libgpiod, and a
    Raspberry Pi -- whose base image already has rpi.gpio -- keeps it, because
    an installed alternative satisfies the clause without pulling the other one.

    How a regression manifests: reordering brings back a board whose install
    reports success and whose display never initializes.
    """
    text = CONTROL.read_text()
    match = re.search(r"^Depends:(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no Depends field in {CONTROL}"
    gpio_clause = next(
        clause for clause in match.group(1).split(",") if "python3-rpi.gpio" in clause
    )
    ordered = [
        alternative.split("(")[0].strip() for alternative in gpio_clause.split("|")
    ]
    assert ordered.index("python3-libgpiod") < ordered.index("python3-rpi.gpio"), (
        "python3-libgpiod must come first, or apt installs rpi.gpio on a board "
        f"whose e-paper driver imports gpiod; got {ordered}"
    )

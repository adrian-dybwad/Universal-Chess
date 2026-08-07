"""Tests for the install-tree privilege boundary the postinst establishes.

Root cause these guard: the postinst used to run

    chown -R "$PRIMARY_USER:$PRIMARY_USER" /opt/universalchess

which handed the service user write access to the *entire* install tree. That
tree contains ``scripts/``, and every helper in it is the target of a
passwordless sudo grant (``install-update``, ``bt-admin``, ``uc-set-timezone``,
``centaur-import-mount``, ``uc-engine-deps``, ``uc-build-memory``,
``centaur-armhf-setup``). Write access to a file that root will execute on
demand is equivalent to root: a compromised web process could rewrite
``bt-admin`` and invoke its own code as root through the existing grant. The
same applied to the Python sources the root-run units execute and to
``config/ssl``, whose private keys became readable by the service user.

The fix inverts the default: the tree is root-owned, and only the paths the
running product genuinely writes are handed to the service user. These tests
derive that writable set from the path constants the application actually writes
through, so the postinst cannot drift from the code -- a new runtime write path
added to ``paths.py`` without a matching grant fails here rather than in the
field.

The counterpart risk is over-restriction: making a path root-owned that the
service *does* write breaks the product on device, where it is far more expensive
to discover. ``test_ca_certificate_stays_readable_by_the_service_user`` covers
the specific instance of that which is easy to get wrong.
"""

from pathlib import Path

import pytest

from universalchess import paths
from universalchess.services import update_service as us

POSTINST = (
    Path(us.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)


def _relative_to_install_root(absolute_path) -> str:
    """Return ``absolute_path`` expressed relative to the install root.

    The postinst refers to paths as ``${DGTCM_PATH}/<relative>``, while the
    application holds them as absolute ``/opt/universalchess/...`` constants.
    Deriving one from the other keeps the assertions tied to the constants the
    code writes through instead of restating string literals that could drift.
    """
    text = str(absolute_path)
    assert text.startswith(paths.BASE_DIR), f"{text} is not under {paths.BASE_DIR}"
    return text[len(paths.BASE_DIR) :].lstrip("/")


# Directories the running product creates, updates or deletes files inside. Each
# is sourced from the constant the writing code uses (see the module docstring in
# paths.py and services/update_service.py).
RUNTIME_WRITABLE_DIRS = [
    _relative_to_install_root(p)
    for p in (
        paths.CONFIG_DIR,      # centaur.ini + atomic temp siblings, engine .uci profiles, JSON stores
        paths.DB_DIR,          # SQLite game database and its journal
        paths.ENGINES_DIR,     # engine binaries, weights, launcher shims, stockfish symlink
        paths.TMP_DIR,         # runtime scratch: state files, build trees, import staging
        paths.WEB_STATIC_DIR,  # epaper.jpg, rewritten on every panel refresh
        us.PENDING_DEB_DIR,    # staged OTA .deb downloads
    )
]

# Paths that must NOT be service-user writable. scripts/ is the escalation
# vector; .venv/ is executed by the root-run units.
ROOT_OWNED_DIRS = [_relative_to_install_root(paths.SCRIPTS_DIR), ".venv"]

UPDATE_STATE_FILE = _relative_to_install_root(us.STATE_FILE)
TLS_DIR = f"{_relative_to_install_root(paths.CONFIG_DIR)}/ssl"


@pytest.fixture
def postinst_text() -> str:
    """The shipped postinst; a missing file means no install-time configuration."""
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    return POSTINST.read_text()


def _service_user_chown_targets(text: str) -> list:
    """Return every chown line that grants ownership to the service user.

    Used to assert on what is handed to the service user without depending on
    the exact flags or quoting of each individual chown call.
    """
    targets = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "chown" not in stripped:
            continue
        if "$PRIMARY_USER" not in stripped and "PRIMARY_USER" not in stripped:
            continue
        targets.append(stripped)
    return targets


def _declared_writable_paths(text: str, array_name: str) -> list:
    """Return the entries of a bash array declared as ``NAME=( ... )``.

    The postinst declares the writable paths once and loops over them, so the
    declaration -- not the individual chown calls -- is where the grant is
    expressed. Parsing it keeps these tests aligned with that structure instead
    of requiring one chown line per path, which would push the script back
    towards repeating itself.
    """
    marker = f"{array_name}=("
    assert marker in text, f"{array_name} declaration missing from postinst"
    start = text.index(marker) + len(marker)
    body = text[start : text.index(")", start)]
    entries = []
    for raw_line in body.splitlines():
        entry = raw_line.split("#", 1)[0].strip().strip('"')
        if entry:
            entries.append(entry)
    return entries


def test_postinst_does_not_hand_the_whole_install_tree_to_the_service_user(postinst_text):
    """No chown may grant the service user ${DGTCM_PATH} itself.

    Why this test exists: this is the privilege-escalation regression. A recursive
    grant on the install root makes every sudo-granted helper in scripts/ writable
    by the service user, so compromising the web process yields root.

    How a regression manifests: a line like
    ``chown -R "$PRIMARY_USER:$PRIMARY_USER" "${DGTCM_PATH}"`` reappears (often
    reintroduced as a "fix permissions" catch-all) and this assertion fails,
    naming the offending line.
    """
    # Match the install root as a whole argument, so a trailing `2>/dev/null ||
    # true` (present on one of the original calls) cannot hide the grant, while a
    # legitimate subpath like ${DGTCM_PATH}/config is still allowed through.
    install_root_arguments = {
        "${DGTCM_PATH}",
        '"${DGTCM_PATH}"',
        "$DGTCM_PATH",
        '"$DGTCM_PATH"',
    }
    offenders = [
        line
        for line in _service_user_chown_targets(postinst_text)
        if install_root_arguments & set(line.split())
    ]
    assert offenders == [], (
        "postinst grants the service user the entire install tree, which makes the "
        f"sudo-granted helpers in {ROOT_OWNED_DIRS[0]}/ writable by that user: {offenders}"
    )


def test_install_tree_is_explicitly_root_owned(postinst_text):
    """The tree must be actively reset to root, not merely left alone.

    Why this test exists: dpkg unpacks as root, but an *upgrade* from a version
    that ran the old blanket chown leaves the on-disk tree owned by the service
    user. Without an explicit reset the escalation persists across upgrades on
    exactly the boards that were already exposed.

    How a regression manifests: dropping the root chown leaves upgraded boards
    with a service-user-owned scripts/ while fresh installs look correct, so the
    vulnerability survives only where it already existed.
    """
    assert 'chown -R root:root "${DGTCM_PATH}"' in postinst_text


@pytest.mark.parametrize("relative_path", RUNTIME_WRITABLE_DIRS)
def test_runtime_write_paths_are_granted_to_the_service_user(postinst_text, relative_path):
    """Every directory the product writes at runtime must be service-user owned.

    Why this test exists: the boundary is only safe if it is also complete.
    Root-owning a directory the service writes breaks the product on device --
    settings fail to save, engines fail to install, the OTA download cannot be
    staged. Parameterised over the constants the writing code uses so adding a
    new runtime path without granting it fails here.

    How a regression manifests: the grant for one path is dropped and that
    feature fails at runtime with EACCES while everything else keeps working,
    which is hard to attribute without this test naming the path.
    """
    declared = _declared_writable_paths(postinst_text, "RUNTIME_WRITABLE_DIRS")
    assert relative_path in declared, (
        f"{relative_path} is written at runtime but is not declared writable; "
        f"postinst declares {declared}"
    )


def test_declared_writable_paths_are_actually_chowned_to_the_service_user(postinst_text):
    """The declared paths must be handed to the service user, not just listed.

    Why this test exists: the per-path tests above check the declaration, so on
    their own they would still pass if the loop that acts on it were deleted or
    changed to chown somewhere else. This pins the mechanism that turns the
    declaration into ownership.

    How a regression manifests: the array stays correct but nothing consumes it,
    so every runtime path silently inherits root from the tree-wide reset and the
    product fails to write anything on device.
    """
    grants = _service_user_chown_targets(postinst_text)
    assert any(
        "RUNTIME_WRITABLE_DIRS" not in line and "${relative_path}" in line for line in grants
    ), "no chown applies the declared writable paths to the service user"
    assert 'for relative_path in "${RUNTIME_WRITABLE_DIRS[@]}"' in postinst_text


@pytest.mark.parametrize("relative_path", ROOT_OWNED_DIRS)
def test_sudo_target_and_venv_directories_are_never_granted_to_the_service_user(
    postinst_text, relative_path
):
    """scripts/ and .venv/ must never appear as a service-user chown target.

    Why this test exists: these are the two trees whose contents root executes.
    Service-user write access to scripts/ is root code execution via the existing
    sudo grants; write access to .venv/ means replacing the interpreter and
    libraries that the root-run TLS unit executes.

    How a regression manifests: a well-meant ``chown -R $PRIMARY_USER
    ${DGTCM_PATH}/scripts`` (e.g. to fix a +x problem) silently restores the
    escalation.
    """
    declared = _declared_writable_paths(postinst_text, "RUNTIME_WRITABLE_DIRS")
    assert relative_path not in declared, (
        f"{relative_path} is declared runtime-writable, but root executes its contents"
    )

    offenders = [
        line for line in _service_user_chown_targets(postinst_text) if relative_path in line
    ]
    assert offenders == [], (
        f"{relative_path} must stay root-owned; root executes its contents: {offenders}"
    )


def test_tls_material_is_reclaimed_by_root_after_the_config_grant(postinst_text):
    """config/ssl must be re-owned to root *after* config/ is granted.

    Why this test exists: config/ needs a recursive grant for centaur.ini and the
    engine profiles, and config/ssl sits inside it -- so the recursive grant also
    hands over the CA and server private keys, letting a compromised service user
    mint trusted certificates for the device. Reclaiming ssl/ afterwards is what
    makes the recursive grant safe, so ordering is the whole point.

    How a regression manifests: if the reclaim moves above the config grant (or is
    removed), the keys end up service-user owned again. Ownership looks plausible
    on inspection, so only the ordering assertion catches it.
    """
    config_dir = _relative_to_install_root(paths.CONFIG_DIR)
    assert config_dir in _declared_writable_paths(postinst_text, "RUNTIME_WRITABLE_DIRS")

    # Assert within setPermissions, the final ownership pass, so the ordering
    # checked here is the one that determines on-disk state after install.
    start = postinst_text.index("function setPermissions")
    block = postinst_text[start : postinst_text.index("\nfunction ", start + 1)]

    root_reset = block.index('chown -R root:root "${DGTCM_PATH}"')
    grant = block.index("grantRuntimeDataOwnership")
    reclaim = block.index(f'chown -R root:root "${{DGTCM_PATH}}/{TLS_DIR}"')

    assert root_reset < grant, (
        "the tree-wide root reset runs after the service-user grant, which would "
        "strip the runtime data paths the product needs to write"
    )
    assert grant < reclaim, (
        "config/ssl is reclaimed by root before config/ is granted, so the recursive "
        "config grant re-exposes the TLS private keys"
    )


def test_ca_certificate_stays_readable_by_the_service_user(postinst_text):
    """The TLS directory must not be locked to root-only access.

    Why this test exists: the web app runs as the service user and reads
    ``config/ssl/rootCA.pem`` to serve the CA download and the iOS mobileconfig
    profile. Hardening ssl/ to 0700 would root-own the keys *and* break that
    endpoint -- a plausible-looking over-correction that only shows up on device.
    The private keys are already 0600 (set by tls.py), so directory traversal can
    stay open without exposing them.

    How a regression manifests: ssl/ is chmodded 0700/0600 and certificate
    download returns a permission error while TLS itself keeps working, so the
    breakage looks unrelated to the permission change.
    """
    for forbidden in (
        f'chmod 700 "${{DGTCM_PATH}}/{TLS_DIR}"',
        f'chmod -R 700 "${{DGTCM_PATH}}/{TLS_DIR}"',
        f'chmod 600 "${{DGTCM_PATH}}/{TLS_DIR}"',
    ):
        assert forbidden not in postinst_text, (
            f"{forbidden} would stop the service user reading rootCA.pem, breaking "
            "the CA download and mobileconfig endpoints"
        )


def test_postinst_precompiles_bytecode_into_the_root_owned_tree(postinst_text):
    """The postinst must precompile the package to .pyc as root.

    Why this test exists: bytecode caching used to happen lazily, because the tree
    was service-user owned and the first import could write __pycache__. A
    root-owned tree removes that ability, so without precompilation every boot
    re-parses every module from source -- a startup cost on a Pi, and a silent
    side effect of the ownership change rather than an intended one. Compiling at
    install time puts root-owned, readable caches in place instead.

    How a regression manifests: no functional failure, just slower board startup,
    which is exactly the kind of change that goes unnoticed without a test.
    """
    assert "compileall" in postinst_text


def test_bytecode_precompilation_skips_the_virtualenv(postinst_text):
    """compileall must exclude .venv from the walk.

    Why this test exists: the virtualenv holds roughly fourteen times as many
    modules as the application (3822 vs 268 in a representative install), and pip
    already byte-compiles them when it installs them. Walking it again adds that
    work to every OTA install on hardware where it is slowest, for no benefit --
    an install-time cost users would feel directly.

    How a regression manifests: no failure, just a noticeably longer update on a
    Pi, which is easy to misattribute to the download or to apt.
    """
    compile_lines = [
        line.strip()
        for line in postinst_text.splitlines()
        if "compileall" in line and not line.strip().startswith("#")
    ]
    assert compile_lines, "compileall is only mentioned in comments"
    for line in compile_lines:
        assert "-x" in line and ".venv" in line, (
            f"compileall must skip .venv, which pip has already compiled: {line}"
        )


def test_bytecode_precompilation_cannot_abort_the_install(postinst_text):
    """compileall failure must not fail the package install.

    Why this test exists: the postinst runs under ``set -euo pipefail``, and
    ``compileall`` exits non-zero if *any* file fails to compile. A single
    unparseable file anywhere in the tree would abort the install and leave the
    package half-configured. Bytecode is an optimisation, so its failure must
    degrade to "slower startup", never "install failed".

    How a regression manifests: dropping the guard turns any stray syntax error
    into a failed install with a dpkg error, on every board.
    """
    compile_lines = [
        line.strip()
        for line in postinst_text.splitlines()
        if "compileall" in line and not line.strip().startswith("#")
    ]
    assert compile_lines, "compileall is only mentioned in comments"
    for line in compile_lines:
        assert "|| true" in line, (
            f"compileall must not abort the install under `set -e`: {line}"
        )


def test_update_state_file_is_granted_to_the_service_user(postinst_text):
    """update-state.json sits at the install root and must be service-user owned.

    Why this test exists: it is the one runtime-written *file* outside the granted
    directories, so a directory-only grant misses it. The service writes it on
    every update check and channel change; root's install-update helper also
    clears it after installing. Root-owning it makes update checks fail to
    persist.

    How a regression manifests: the file inherits root from the tree-wide reset,
    update checks silently fail to save, and the pending-update state is lost on
    restart.
    """
    declared = _declared_writable_paths(postinst_text, "RUNTIME_WRITABLE_FILES")
    assert UPDATE_STATE_FILE in declared, (
        f"{UPDATE_STATE_FILE} is written at runtime but is not declared writable; "
        f"postinst declares {declared}"
    )
    assert 'for relative_path in "${RUNTIME_WRITABLE_FILES[@]}"' in postinst_text

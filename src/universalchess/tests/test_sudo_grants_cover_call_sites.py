"""Every privileged command the app runs must be one the package actually grants.

Three shipped features did nothing on a board without a hand-added blanket
NOPASSWD rule, all for the same reason: the code called a system binary through
``sudo`` and the postinst granted no matching sudoers rule. Reboot and Shutdown
ran ``sudo systemctl reboot`` / ``poweroff``; Wi-Fi scan, connect and forget ran
``sudo iwlist`` / ``sudo nmcli``; the radio toggle ran ``sudo rfkill``. Each was
denied, and because none of them checked the exit status the UI reported success
while nothing happened.

Nothing connected the call sites to the grants, so the drift was invisible: the
grants live in a shell script and the calls live in argv lists spread across the
package. Reviewing it by eye does not work either -- the first grep written to
audit this missed the ``rfkill`` sites because they use single quotes and the
``os.system`` sites because they are not argv lists at all.

This test closes that gap by parsing both sides. It walks the app's syntax trees
for every ``sudo`` invocation, parses the NOPASSWD rules out of the postinst, and
requires each call to a named system binary to match a grant. Sites that are
deliberately ungranted are named in one registry with the reason, so a
best-effort path stays legible while a new ungranted call still fails the build;
a registry entry whose call site has gone away fails too, so the list cannot rot
into cover for a call that comes back.

Only literal command names are checked. The pinned-helper calls
(``sudo -n /opt/universalchess/scripts/...``) reach the helper through a module
constant, and each already has a dedicated test tying its grant to its caller.
The bug this guards against has only ever come from invoking a system binary
directly.
"""

from __future__ import annotations

import ast
import re
import shlex
from functools import cache
from pathlib import Path
from typing import NamedTuple

import universalchess

APP_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent.parent
POSTINST = REPO_ROOT / "packaging" / "deb-root" / "DEBIAN" / "postinst"

# Directories holding no privileged behaviour the package ships: the tests, and
# the vendor-protocol simulators. Nothing imports either simulator; both are
# ``__main__`` scripts a developer runs by hand from a shell, where sudo has a
# terminal and can prompt. The in-app BLE path (managers/ble.py) reaches the same
# btmgmt settings through the pinned bt-admin helper, which is granted.
EXCLUDED_DIRS = frozenset({"tests", "simulators"})

SUDO = "sudo"
NON_INTERACTIVE_FLAG = "-n"

# Privileged call sites this check does not enforce, each with the reason. Both
# assertions below read this one registry: a site that has no grant also cannot
# usefully be non-interactive, and splitting them would mean stating the same
# reason twice.
#
# An entry is a claim about what the operator gets, not a way to silence the
# check. Keyed by file and command so it survives line-number churn.
UNENFORCED: dict[tuple[str, str], str] = {
    # The postinst refuses these grants by design, in its own words: "granting
    # NOPASSWD on apt/systemd-run is equivalent to unrestricted root". A dpkg
    # database left mid-configure is repaired by an operator over SSH; the
    # in-app attempt is best-effort and logs its own failure.
    ("services/apt_recovery.py", "dpkg"): "no grant by design -- root-equivalent",
    ("services/apt_recovery.py", "apt-get"): "no grant by design -- root-equivalent",
    ("services/apt_recovery.py", "systemd-run"): "no grant by design -- root-equivalent",
    # The translate-mode serial tap parks the real serial node behind a PTY so the
    # original Centaur binary can be watched. Ten privileged steps on /dev, none
    # granted; they want one pinned helper (the bt-admin pattern) that performs
    # exactly this swap, which is not written yet. Reached from
    # board/sync_centaur.py via heal_swapped_serial_node and from the tap's own
    # setup/restore, so this is a live path, not dead code.
    ("services/centaur_serial/relay.py", "mv"): "owed a pinned relay helper",
    ("services/centaur_serial/relay.py", "chmod"): "owed a pinned relay helper",
    ("services/centaur_serial/relay.py", "fuser"): "owed a pinned relay helper",
    ("services/centaur_serial/relay.py", "stty"): "owed a pinned relay helper",
    ("services/centaur_serial/relay.py", "ln"): "owed a pinned relay helper",
    ("services/centaur_serial/relay.py", "rm"): "owed a pinned relay helper",
    ("services/centaur_serial/relay.py", "systemctl"): "owed a pinned relay helper",
    # Direct mode runs the original Centaur binary as root from its own directory.
    # sudoers authorizes a resolved absolute path, so a relative "./centaur" under
    # a caller-chosen working directory cannot be expressed as a grant at all --
    # closing this needs a helper that launches one pinned path.
    ("main.py", "./centaur"): "relative path is not expressible as a grant",
    ("main.py", "pkill"): "owed a pinned helper alongside the direct-mode launch",
}


class SudoCall(NamedTuple):
    """One ``sudo`` invocation found in the source.

    Attributes:
        relative_path: Path of the containing file, relative to the app package.
        line: Line number of the invocation.
        command: The binary sudo is asked to run, or None when built at runtime.
        args: Arguments after the command; None for any built at runtime.
        argv: The full literal argv as written, for the failure message.
    """

    relative_path: str
    line: int
    command: str | None
    args: tuple[str | None, ...]
    argv: tuple[str | None, ...]

    @property
    def key(self) -> tuple[str, str]:
        """Registry key: the file and command, which survive line-number churn."""
        return (self.relative_path, self.command or "<dynamic>")

    @property
    def command_name(self) -> str | None:
        """The command's basename, which is how a grant is matched.

        sudo resolves a bare command name through its own secure_path, so a call
        writing ``chpasswd`` and a grant writing ``/usr/sbin/chpasswd`` name the
        same command. Comparing basenames on both sides is what makes the two
        spellings meet.
        """
        return Path(self.command).name if self.command else None

    def describe(self) -> str:
        """One-line rendering used in assertion messages."""
        written = " ".join("<dynamic>" if part is None else part for part in self.argv)
        return f"{self.relative_path}:{self.line}: {written}"


@cache
def _module_constants(source_path: Path) -> dict[str, ast.expr]:
    """Top-level ``NAME = <expr>`` assignments in one module, unevaluated."""
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
    return constants


@cache
def _imported_from(source_path: Path) -> dict[str, Path]:
    """Where each ``from universalchess... import NAME`` in a module comes from.

    Only first-party imports are followed, and only to a module file inside the
    package: the helper paths are defined in :mod:`universalchess.paths` and used
    elsewhere, so resolving a call's command needs exactly this one hop.
    """
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    origins: dict[str, Path] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("universalchess."):
            continue
        relative = node.module[len("universalchess.") :].replace(".", "/")
        candidate = APP_ROOT / f"{relative}.py"
        if candidate.exists():
            for alias in node.names:
                origins[alias.asname or alias.name] = candidate
    return origins


def _resolve(  # noqa: C901, PLR0911 - one branch per AST node form; a dispatch
    # table would hide which forms are handled, and each branch is two lines.
    node: ast.expr,
    source_path: Path,
    depth: int = 0,
) -> str | None:
    """The node's string value if it can be determined statically, else None.

    Handles the forms the pinned helper paths actually use: a literal, an
    f-string built from other constants (``f"{SCRIPTS_DIR}/bt-admin"``), a
    ``Path`` join, and a name defined in this module or imported from another. A
    command assembled at runtime resolves to None and is reported as dynamic
    rather than guessed at.
    """
    if depth > 8:  # a constant cycle; nothing legitimate nests this deep
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = [_resolve(value, source_path, depth + 1) for value in node.values]
        return None if None in parts else "".join(parts)  # type: ignore[arg-type]
    if isinstance(node, ast.FormattedValue):
        return _resolve(node.value, source_path, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _resolve(node.left, source_path, depth + 1)
        right = _resolve(node.right, source_path, depth + 1)
        return None if left is None or right is None else f"{left}/{right}"
    if isinstance(node, ast.Call):
        # Path("...") and str(...) wrap a value without changing it.
        if node.args and isinstance(node.func, ast.Name) and node.func.id in {"Path", "str"}:
            return _resolve(node.args[0], source_path, depth + 1)
        return None
    if isinstance(node, ast.Name):
        local = _module_constants(source_path).get(node.id)
        if local is not None:
            return _resolve(local, source_path, depth + 1)
        origin = _imported_from(source_path).get(node.id)
        if origin is not None:
            imported = _module_constants(origin).get(node.id)
            if imported is not None:
                return _resolve(imported, origin, depth + 1)
    return None


def _argv_from_sequence(
    node: ast.List | ast.Tuple, source_path: Path
) -> tuple[str | None, ...] | None:
    """Resolved argv for a list/tuple whose first element is ``sudo``, else None."""
    if not node.elts or _resolve(node.elts[0], source_path) != SUDO:
        return None
    return tuple(_resolve(element, source_path) for element in node.elts)


def _argv_from_command_string(node: ast.Constant) -> tuple[str | None, ...] | None:
    """Literal argv for a shell string starting with ``sudo``, else None.

    Covers the ``os.system("sudo ...")`` form, which no argv-list scan would see.
    """
    text = node.value
    if not isinstance(text, str) or not text.startswith(f"{SUDO} "):
        return None
    try:
        return tuple(shlex.split(text))
    except ValueError:
        # An unbalanced quote means it is not a command line this scan can read;
        # report it as fully dynamic rather than dropping the call site.
        return (SUDO, None)


def _find_sudo_calls(source_path: Path) -> list[SudoCall]:
    """Every ``sudo`` invocation in one module, as argv lists or shell strings."""
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    relative_path = str(source_path.relative_to(APP_ROOT))
    calls: list[SudoCall] = []
    for node in ast.walk(tree):
        # isinstance does not accept PEP 604 unions on Python 3.9.
        if isinstance(node, (ast.List, ast.Tuple)):
            argv = _argv_from_sequence(node, source_path)
        elif isinstance(node, ast.Constant):
            argv = _argv_from_command_string(node)
        else:
            continue
        if argv is None:
            continue
        rest = [part for part in argv[1:] if part != NON_INTERACTIVE_FLAG]
        calls.append(
            SudoCall(
                relative_path=relative_path,
                line=node.lineno,
                command=rest[0] if rest else None,
                args=tuple(rest[1:]),
                argv=argv,
            )
        )
    return calls


def _app_sources() -> list[Path]:
    """Shipped app modules, excluding the test package."""
    return [
        path
        for path in sorted(APP_ROOT.rglob("*.py"))
        if not EXCLUDED_DIRS & set(path.relative_to(APP_ROOT).parts[:-1])
    ]


def _sudo_calls() -> list[SudoCall]:
    """Every ``sudo`` invocation across the shipped app."""
    return [call for source in _app_sources() for call in _find_sudo_calls(source)]


def _postinst_variables(text: str) -> dict[str, str]:
    """Resolved ``NAME=value`` shell assignments from the postinst.

    The grants name their helper through variables (``$INSTALL_HELPER``, itself
    built from ``${DGTCM_PATH}``), so the values have to be expanded before a
    grant can be compared with a call site. Repeated passes resolve chains;
    assignments only ever refer to variables set above them.
    """
    variables: dict[str, str] = {}
    for name, raw in re.findall(r'^\s*([A-Z_][A-Z0-9_]*)="([^"]*)"', text, re.MULTILINE):
        value = raw
        for _ in range(len(variables) + 1):
            expanded = re.sub(
                r"\$\{?([A-Z_][A-Z0-9_]*)\}?",
                lambda match: variables.get(match.group(1), match.group(0)),
                value,
            )
            if expanded == value:
                break
            value = expanded
        variables[name] = value
    return variables


def _granted_commands(text: str) -> set[tuple[str, tuple[str, ...]]]:
    """Every NOPASSWD grant in the postinst as (command basename, arguments).

    Matched on the basename because sudo resolves a bare command name through
    its own secure_path: the code writes ``chpasswd`` and the grant writes
    ``/usr/sbin/chpasswd``, and sudo treats those as the same command.
    """
    variables = _postinst_variables(text)
    grants: set[tuple[str, tuple[str, ...]]] = set()
    for raw in re.findall(r"NOPASSWD:\s*([^\"']+)", text):
        expanded = re.sub(
            r"\$\{?([A-Z_][A-Z0-9_]*)\}?",
            lambda match: variables.get(match.group(1), match.group(0)),
            raw.strip(),
        )
        parts = shlex.split(expanded)
        if parts:
            grants.add((Path(parts[0]).name, tuple(parts[1:])))
    return grants


def test_postinst_declares_the_grants_this_check_reads():
    """The grant parser finds the postinst's rules.

    Why this test exists: every other assertion here compares call sites against
    the parsed grants, so a parser that silently returned nothing would turn both
    checks into "no grants exist, so nothing can match" -- or, worse, an empty
    expected set that passes. The chpasswd rule is the one grant written with a
    literal absolute path and no shell variable, so it pins the parse itself.

    Failure: the postinst moved its grants, or the shell quoting changed, and the
    parser needs updating before the checks below mean anything.
    """
    grants = _granted_commands(POSTINST.read_text())
    assert grants, "no NOPASSWD grants parsed from the postinst"
    assert ("chpasswd", ()) in grants
    helper_grants = {command for command, _ in grants if command.startswith(("uc-", "bt-"))}
    assert helper_grants, f"no pinned-helper grants resolved; got {sorted(grants)}"


def test_every_registered_exemption_still_has_a_call_site():
    """The registry may not outlive the calls it excuses.

    Why this test exists: an exemption is scoped to one file and one command. If
    the call is removed or rewritten to go through a helper and the entry stays,
    it silently pre-authorizes the same ungranted command being reintroduced
    there later -- the check would pass on the very defect it exists to catch.

    Failure: an entry names a file/command pair that no longer appears. Delete the
    entry; the privileged call it described is gone.
    """
    live = {call.key for call in _sudo_calls()}
    stale = sorted(key for key in UNENFORCED if key not in live)
    assert not stale, "exemptions with no matching call site:\n" + "\n".join(
        f"  {path}: {command}" for path, command in stale
    )


def test_every_sudo_call_site_is_non_interactive():
    """No sudo call may omit -n, so a missing grant fails fast and loud.

    Why this test exists: without -n, sudo under a service tries to prompt for a
    password it can never read. The command fails with "no tty present" -- or
    hangs until a timeout -- rather than sudo's immediate "a password is
    required", which is why the reboot and Wi-Fi failures read as the feature
    doing nothing instead of as a missing grant.

    Failure: a new call site drops the flag, so the next missing grant is a
    silent no-op again. Add -n, or register the site with the reason it prompts.
    """
    offenders = [
        call
        for call in _sudo_calls()
        if NON_INTERACTIVE_FLAG not in call.argv and call.key not in UNENFORCED
    ]
    assert not offenders, "sudo invoked interactively:\n" + "\n".join(
        call.describe() for call in sorted(offenders, key=lambda call: call.key)
    )


def test_every_sudo_call_to_a_system_binary_has_a_matching_grant():
    """Each system binary run under sudo is covered by a postinst grant.

    Why this test exists: this is the defect itself. A privileged call shipped
    with no sudoers rule is denied on every board that has not had a blanket
    NOPASSWD rule added by hand, and the feature quietly does nothing. Arguments
    are compared too, because sudo authorizes the command plus its argv: a grant
    for ``systemctl restart universal-chess.service`` does not authorize
    ``systemctl stop`` anything.

    Failure: a call names a binary, or an argument list, that no grant covers.
    Route it through a pinned helper and grant that, or register the site with
    what the operator sees when it is denied.
    """
    grants = _granted_commands(POSTINST.read_text())
    granted_commands = {command for command, _ in grants}
    offenders = []
    for call in _sudo_calls():
        if call.command is None or call.key in UNENFORCED:
            continue
        if call.command_name not in granted_commands:
            offenders.append(call)
            continue
        # The command is granted; the argument list still has to match one rule.
        # A grant naming no arguments authorizes any (that is sudoers' own rule,
        # and how the pinned helpers are granted), so an empty args tuple matches.
        if not any(
            command == call.command_name and call.args[: len(args)] == args
            for command, args in grants
        ):
            offenders.append(call)
    assert not offenders, "sudo command with no matching grant:\n" + "\n".join(
        call.describe() for call in sorted(offenders, key=lambda call: (call.key, call.line))
    )

#!/usr/bin/env python3
"""Build the single-file version of the SD card tool that users download.

The tool is written as several modules because that is what makes it testable:
pure transformations in ``bootfs`` and ``hostdns``, side effects confined to
``hostcheck`` and ``console``. None of that helps someone who just wants to
prepare a card, and telling them to clone a repository to run one script is a
poor trade. This produces one file they can read and run.

The output is plain, readable Python rather than an archive. Anyone about to run
something that writes to their SD card should be able to look at it first, which
rules out both a zipapp and embedding the sources as opaque string literals.

How it works: the modules share one namespace, so their sources are concatenated
in dependency order and each module name is then bound to the resulting module
itself. That makes an existing ``bootfs.parse_cloud_config(...)`` call resolve to
the inlined function without rewriting a single call site.

That trick is only sound while no two modules define the same top-level name, so
this refuses to build when they do. Silently letting one definition win would
produce a file that runs and misbehaves, which is far worse than a failed build.
"""

from __future__ import annotations

import argparse
import ast
import collections
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Dependency order: each module may only reference those before it.
MODULES = ("console.py", "bootfs.py", "hostdns.py", "hostcheck.py")
ENTRY_POINT = "enable_usb_gadget.py"
MOTD_SCRIPT = "motd-dns-check.sh"

DEFAULT_OUTPUT = "enable_usb_gadget_standalone.py"

# Imports of the tool's own modules, which have no meaning once inlined.
INTERNAL_NAMES = {Path(name).stem for name in MODULES} | {Path(ENTRY_POINT).stem}

_FUTURE_IMPORT = "from __future__ import annotations"
EMBED_TARGET = "EMBEDDED_MOTD_CHECK_SCRIPT: str | None = None"
_SHEBANG = "#!/usr/bin/env python3"


class CollisionError(Exception):
    """Raised when two modules define the same top-level name."""


def top_level_names(source: str) -> set[str]:
    """Return the names a module binds at its top level."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_no_collisions(sources: dict[str, str]) -> None:
    """Raise if any top-level name is defined by more than one module.

    Raises:
        CollisionError: If a name is bound twice. The merged file would keep
            only the last definition, and every earlier caller would silently
            get the wrong one -- a fault that survives every test run against
            the modular sources, because there the two names are distinct.

    """
    owners: dict[str, list[str]] = collections.defaultdict(list)
    for name, source in sources.items():
        for symbol in top_level_names(source):
            owners[symbol].append(name)

    clashes = {s: o for s, o in owners.items() if len(o) > 1}
    if clashes:
        detail = "\n".join(f"  {s} defined in {', '.join(o)}" for s, o in sorted(clashes.items()))
        message = (
            "Cannot merge: these names are defined in more than one module.\n"
            f"{detail}\n"
            "Move the shared definition into one module and import it."
        )
        raise CollisionError(message)


def strip_module_scaffolding(source: str) -> str:
    """Remove the parts of a module that only make sense as a separate file.

    Drops the shebang, the ``__future__`` import and imports of sibling modules;
    all three are re-established once, at the top of the merged file. Ordinary
    standard-library imports are left where they are: Python allows them at any
    point, repeating one is harmless, and rewriting them risks changing meaning
    for no gain in a file nobody edits.
    """
    kept: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped == _FUTURE_IMPORT or stripped.startswith(_SHEBANG):
            continue
        if re.fullmatch(rf"import ({'|'.join(sorted(INTERNAL_NAMES))})", stripped):
            continue
        if re.match(rf"from ({'|'.join(sorted(INTERNAL_NAMES))}) import ", stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def _module_docstring_removed(source: str) -> str:
    """Return ``source`` without its leading module docstring.

    Each module's docstring documents it as a module. Concatenated, they would
    read as a pile of unrelated prose under a single heading, so the merged file
    carries one header of its own and keeps each module's under its banner
    instead.
    """
    tree = ast.parse(source)
    if not (tree.body and isinstance(tree.body[0], ast.Expr)):
        return source
    first = tree.body[0]
    if not isinstance(first.value, ast.Constant) or not isinstance(first.value.value, str):
        return source
    lines = source.splitlines()
    return "\n".join(lines[first.end_lineno :]).strip("\n")


def _banner(title: str) -> str:
    """Return a comment banner separating one inlined module from the next."""
    rule = "# " + "-" * 74
    return f"{rule}\n# {title}\n{rule}"


def embed_motd_script(entry_source: str, script: str) -> str:
    """Replace the entry point's embed placeholder with the script's text.

    Raises:
        ValueError: If the placeholder is absent. Without it the built file
            would fall back to reading the script from a path that does not
            exist next to a standalone download, and fail at the moment someone
            tries to prepare a card.

    """
    if EMBED_TARGET not in entry_source:
        message = (
            f"Expected to find {EMBED_TARGET!r} in {ENTRY_POINT}. "
            "The single-file build needs it to embed the MOTD script."
        )
        raise ValueError(message)
    # A repr() of the text is unreadable for a 60-line shell script. A triple
    # quoted literal keeps it legible, which is the point of this format, and
    # is safe here because the build refuses any content that could close it.
    if '"""' in script or script.rstrip().endswith("\\"):
        message = (
            f"{MOTD_SCRIPT} contains a triple quote or a trailing backslash, "
            "which cannot be embedded as a readable literal."
        )
        raise ValueError(message)
    literal = f'EMBEDDED_MOTD_CHECK_SCRIPT: str | None = """\\\n{script}"""'
    return entry_source.replace(EMBED_TARGET, literal)


def build(source_dir: Path = HERE) -> str:
    """Return the complete single-file tool as text.

    Raises:
        CollisionError: If two modules define the same top-level name.
        ValueError: If the entry point cannot be prepared for embedding.

    """
    sources = {name: (source_dir / name).read_text(encoding="utf-8") for name in MODULES}
    entry = (source_dir / ENTRY_POINT).read_text(encoding="utf-8")
    check_no_collisions({**sources, ENTRY_POINT: entry})

    entry = embed_motd_script(entry, (source_dir / MOTD_SCRIPT).read_text(encoding="utf-8"))

    header = f'''{_SHEBANG}
"""Prepare a Raspberry Pi SD card for USB Ethernet gadget access.

Generated file -- do not edit. Built by tools/sd-card-setup/build_single_file.py
from the modules in that directory; change those and rebuild.

Run it on the machine with the card in a reader:

    python3 {DEFAULT_OUTPUT}

It prepares the card in Client mode, where the Pi takes an address from this
machine and so has a route to the internet, then waits for the board and checks
that DNS works over the USB link. Use --shared for a Pi that serves its own DHCP
at 10.12.194.1 and needs nothing configured here, at the cost of that route, or
--auto to leave the vendor's watcher moving between the two. Use --check-dns on
its own to run only the check, against a board already connected.
"""

{_FUTURE_IMPORT}

import sys as _sys
'''

    # Bind each module's own name to this module, so qualified references such
    # as bootfs.parse_cloud_config keep resolving without touching call sites.
    #
    # This has to come before the inlined code, not after it: hostcheck builds
    # its platform table at module level out of hostdns functions, so the name
    # must already resolve while that code runs. Binding the alias to the module
    # object rather than its contents is what makes this work -- the attributes
    # appear on it as execution proceeds.
    aliases = "\n".join(f"{name} = _sys.modules[__name__]" for name in sorted(INTERNAL_NAMES))

    parts = [header, _banner("Module self-aliases, so qualified names still resolve"), aliases]
    for name in (*MODULES, ENTRY_POINT):
        source = entry if name == ENTRY_POINT else sources[name]
        parts.append(_banner(f"{name} (inlined)"))
        parts.append(strip_module_scaffolding(_module_docstring_removed(source)))

    parts.append('if __name__ == "__main__":\n    _sys.exit(main())')
    return "\n\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the single-file tool. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Where to write the built file (default {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Build and verify only; do not write anything.",
    )
    args = parser.parse_args(argv)

    try:
        built = build()
    except (CollisionError, ValueError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    # A file that does not compile is worse than no file, and the failure would
    # otherwise surface only when a user runs it.
    compile(built, args.output, "exec")

    if args.check:
        print(f"Builds cleanly: {len(built.splitlines())} lines")  # noqa: T201
        return 0

    output = Path(args.output)
    output.write_text(built, encoding="utf-8")
    output.chmod(0o755)
    print(f"Wrote {output} ({len(built.splitlines())} lines)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for the single-file build, exercising the artifact users download.

The modular sources are covered thoroughly elsewhere. None of that says
anything about the file people actually run, which is assembled by a separate
process that can fail in ways the sources never would: a module left out, a
qualified name that no longer resolves, a shell script that silently reverted to
being read from a path that will not exist on a standalone download.

So these tests run the built file as a subprocess, from a directory containing
nothing else, which is the only way to prove it stands alone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_single_file

SOURCE_DIR = Path(__file__).resolve().parents[1]
CONFIG_TXT = "dtparam=audio=on\n"
CMDLINE_TXT = "console=serial0,115200 console=tty1 root=PARTUUID=041bba91-02 rootwait\n"
USER_DATA = "#cloud-config\nhostname: dgtcentaur\nusers:\n- name: pa\n"


@pytest.fixture(scope="module")
def built_tool(tmp_path_factory) -> Path:
    """Build the single file into a directory holding nothing else.

    An empty directory is the point: if the tool still needs a sibling module or
    the shell script, importing or running it here fails, which is exactly the
    breakage a user downloading one file would hit.
    """
    directory = tmp_path_factory.mktemp("standalone")
    target = directory / "enable_usb_gadget.py"
    target.write_text(build_single_file.build(SOURCE_DIR), encoding="utf-8")
    return target


@pytest.fixture
def card(tmp_path) -> Path:
    """Return a directory shaped like a freshly imaged Pi boot partition."""
    (tmp_path / "config.txt").write_text(CONFIG_TXT)
    (tmp_path / "cmdline.txt").write_text(CMDLINE_TXT)
    (tmp_path / "user-data").write_text(USER_DATA)
    (tmp_path / "overlays").mkdir()
    (tmp_path / "start.elf").write_bytes(b"\x00")
    return tmp_path


def _run(tool: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the built tool from its own directory and capture its output."""
    return subprocess.run(  # noqa: S603
        [sys.executable, str(tool), *args],
        capture_output=True,
        text=True,
        cwd=tool.parent,
        check=False,
    )


class TestBuiltFileStandsAlone:
    def test_prepares_a_card_with_no_other_files_present(self, built_tool, card):
        """The built file must do the real job with nothing beside it.

        Why this test exists: this is the whole promise of the single-file
        build. Every module it needs has to be inlined, and every qualified name
        such as bootfs.parse_cloud_config has to still resolve once they share
        one namespace.

        How the regression manifests: a NameError or ModuleNotFoundError in
        stderr, which is what happened when the module self-aliases were
        emitted after the inlined code instead of before it.
        """
        result = _run(built_tool, "--boot", str(card), "--dry-run")

        assert result.returncode == 0, result.stderr
        assert "Error" not in result.stderr
        assert "dtoverlay=dwc2,dr_mode=peripheral" in result.stdout
        assert "modules-load=dwc2,g_ether" in result.stdout

    def test_embeds_the_shell_script_rather_than_reading_it(self, built_tool, card):
        """The MOTD script must come from inside the file.

        Why this test exists: the modular tool reads motd-dns-check.sh off the
        disk. If the embed placeholder is ever renamed or dropped, the built
        file falls back to that path, finds nothing next to a standalone
        download, and fails at the moment someone tries to prepare a card. The
        build directory here deliberately does not contain the script.

        How the regression manifests: "Missing required script" in the output,
        or the write_files entry absent from the diff.
        """
        assert not (built_tool.parent / "motd-dns-check.sh").exists()

        result = _run(built_tool, "--boot", str(card), "--dry-run")

        assert "Missing required script" not in result.stdout + result.stderr
        assert "/etc/update-motd.d/98-universal-chess-dns" in result.stdout

    def test_the_folded_in_dns_check_is_reachable(self, built_tool):
        """--check-dns must work in the built file.

        Why this test exists: that mode was a separate script until it was
        folded in, and it is the one the Pi's login banner tells people to run.
        It is reached by a different branch of main than the card path, so the
        card tests above would not catch it being broken.

        How the regression manifests: a non-zero exit with a traceback, rather
        than a diagnosis. The diagnosis itself depends on the host, so only the
        absence of a crash is asserted.
        """
        result = _run(built_tool, "--check-dns")

        assert "Traceback" not in result.stderr
        assert "Host:" in result.stdout

    def test_reports_the_same_card_facts_as_the_modular_tool(self, built_tool, card):
        """The built file must not quietly differ from the sources.

        Why this test exists: a build that drops or reorders code could still
        run while behaving differently from the tested sources, which would make
        every other test in this suite meaningless for the shipped artifact.

        How the regression manifests: the identity block differing between the
        two. Both run under the same interpreter so PyYAML availability, which
        legitimately changes this output, is held constant.
        """
        modular = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(SOURCE_DIR / "enable_usb_gadget.py"),
                "--boot",
                str(card),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        built = _run(built_tool, "--boot", str(card), "--dry-run")

        assert built.stdout == modular.stdout


class TestBuildRefusesToProduceABrokenFile:
    def test_rejects_a_name_defined_in_two_modules(self):
        """A duplicated top-level name must fail the build, loudly.

        Why this test exists: merging modules into one namespace means the last
        definition of a repeated name silently wins, and every earlier caller
        gets the wrong one. Nothing in the modular test suite can catch that,
        because there the two names live in different modules and are correct.
        This guard is the only thing standing between that mistake and a shipped
        file that runs and misbehaves.

        How the regression manifests: no exception raised, meaning the build
        would happily emit the broken merge.
        """
        sources = {
            "a.py": "def confirm():\n    return 1\n",
            "b.py": "def confirm():\n    return 2\n",
        }

        with pytest.raises(build_single_file.CollisionError, match="confirm"):
            build_single_file.check_no_collisions(sources)

    def test_accepts_distinct_names(self):
        """The guard must not fire on the real modules.

        Why this test exists: a collision check that rejects valid input would
        block every build, so the negative case needs pinning alongside the
        positive one. This is the null case for the check.

        How the regression manifests: CollisionError on sources that are fine.
        """
        build_single_file.check_no_collisions(
            {"a.py": "X = 1\ndef one():\n    pass\n", "b.py": "Y = 2\ndef two():\n    pass\n"}
        )

    def test_fails_when_the_embed_placeholder_is_missing(self):
        """A missing placeholder must stop the build, not pass silently.

        Why this test exists: without the placeholder the built file falls back
        to reading the shell script from disk, which fails only later, on a
        user's machine, at the point they try to prepare a card. Failing at
        build time turns a field failure into a CI failure.

        How the regression manifests: no exception, and a built file that cannot
        install the MOTD script.
        """
        with pytest.raises(ValueError, match="single-file build"):
            build_single_file.embed_motd_script("nothing to replace here", "#!/bin/sh\n")

    def test_refuses_a_script_it_cannot_embed_readably(self):
        """Content that would break out of the literal must be rejected.

        Why this test exists: the script is embedded in a triple-quoted literal
        to keep the output readable. A triple quote or trailing backslash in the
        source would terminate or escape that literal, producing a file that
        fails to compile, or worse, one that compiles into something else.

        How the regression manifests: no exception, and a syntactically broken
        or subtly altered build.
        """
        with pytest.raises(ValueError, match="triple quote"):
            build_single_file.embed_motd_script(build_single_file.EMBED_TARGET, 'echo """\n')


class TestBuiltFileShape:
    def test_compiles_as_python(self):
        """The output must at minimum be valid Python.

        Why this test exists: the build assembles text, so a mistake in the
        assembly produces a file that fails at import with a SyntaxError. That
        is cheap to catch here and expensive to discover after release.

        How the regression manifests: SyntaxError from compile.
        """
        compile(build_single_file.build(SOURCE_DIR), "built", "exec")

    def test_carries_no_imports_of_its_own_modules(self):
        """Sibling imports must be stripped, since the siblings are gone.

        Why this test exists: a leftover "import bootfs" makes the file depend
        on the very layout the build exists to remove, and it would only fail
        for the user, who has no bootfs.py.

        How the regression manifests: an import line for an inlined module
        surviving into the output.
        """
        built = build_single_file.build(SOURCE_DIR)

        for name in build_single_file.INTERNAL_NAMES:
            assert f"\nimport {name}\n" not in built
            assert f"\nfrom {name} import " not in built

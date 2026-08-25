"""Shared harness for running scripts/changelog-audit.sh against real history.

The audit's own tests and the tests for the workflows whose commits it judges
both need to build a throwaway repository, commit into it, and read the report.
The harness lives here so neither copy can drift from the other: a section parser
that stopped matching a renamed heading would otherwise keep one suite green while
the other went quiet.

Committer identity and signing are passed per invocation with ``git -c`` rather
than written to any configuration, so callers cannot depend on -- or disturb --
the developer's git setup.
"""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "changelog-audit.sh"

# Committed on the first commit so every later commit has a parent, and so the
# audit range never starts at the root commit (which git cannot diff normally).
BASE_FILE = "seed.txt"

CHANGELOG = "CHANGELOG.md"

# Headers the audit prints. Candidates are split by whether any changelog commit
# follows them in the range, because those two cases need different action: one is
# owed an entry outright, the other needs a prose check. The trailing list names
# the changelog commits themselves -- a commit there is being reported as a source
# of entries, not accused of missing one, and a whole-output substring check
# cannot tell those two apart.
UNDESCRIBED_HEADER = "Undescribed"
POSSIBLY_DESCRIBED_HEADER = "Possibly described"
DECLARED_EXEMPT_HEADER = "Declared exempt"
CHANGELOG_COMMITS_HEADER = "Changelog commits in the range"

# Trailer a commit uses to state that no entry is owed, making a judgement the
# rule already requires in prose readable by the audit as well.
EXEMPT_TRAILER = "Changelog: none"

# Note the audit prints when a Changelog: line exists but not as a git trailer.
MISPLACED_NOTE = "not in the trailer block"


def candidate_section(stdout):
    """Return the part of the audit output listing candidates of either kind."""
    return stdout.split(CHANGELOG_COMMITS_HEADER)[0]


def section(stdout, header, *following_headers):
    """Return the text under ``header``, stopping at any of the later headers."""
    if header not in stdout:
        return ""
    body = stdout.split(header, 1)[1]
    for boundary in (*following_headers, CHANGELOG_COMMITS_HEADER):
        body = body.split(boundary)[0]
    return body


def undescribed_section(stdout):
    """Return only the candidates with no changelog commit after them."""
    return section(
        stdout, UNDESCRIBED_HEADER, POSSIBLY_DESCRIBED_HEADER, DECLARED_EXEMPT_HEADER
    )


def declared_exempt_section(stdout):
    """Return only the commits that declared no entry is owed."""
    return section(stdout, DECLARED_EXEMPT_HEADER)


class Repo:
    """A throwaway git repository the audit can be run against."""

    def __init__(self, path: Path):
        self.path = path

    def git(self, *args, check=True):
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [  # noqa: S607
                "git",
                "-c", "user.email=tests@example.invalid",
                "-c", "user.name=Changelog Audit Tests",
                "-c", "commit.gpgsign=false",
                *args,
            ],
            cwd=str(self.path), capture_output=True, text=True, check=check,
        )

    def commit(self, subject, files, body=None):
        """Create a commit touching every path in ``files``.

        ``body`` is passed as a second paragraph, which is where a trailer block
        has to live to be parsed as one.
        """
        for relative in files:
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a") as handle:
                handle.write(f"{subject}\n")
            self.git("add", relative)
        message = ["-m", subject]
        if body is not None:
            message += ["-m", body]
        self.git("commit", *message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def audit(self, *args):
        return subprocess.run(  # noqa: S603 - test invokes the repo's own script
            ["bash", str(SCRIPT), *args],  # noqa: S607
            cwd=str(self.path), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60, check=False,
        )


def initialized_repo(tmp_path):
    """An initialized repository with one seed commit."""
    fixture = Repo(tmp_path)
    fixture.git("init", "-b", "main")
    fixture.commit("Seed the repository.", [BASE_FILE])
    return fixture

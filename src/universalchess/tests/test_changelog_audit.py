"""Tests for scripts/changelog-audit.sh -- finding changes shipped undescribed.

Why these tests exist:
    Three changes reached main with no changelog entry (move times in exported
    PGN, the screen blanking between splash and menu, and the clock sync-state
    caching). They were found by reading the log by hand before a push. This
    script encodes that audit so it is repeatable; these tests exist because an
    audit that silently reports nothing is worse than no audit -- it converts an
    unnoticed omission into a positive assurance that there is none.

How a regression manifests:
    - An exemption widened too far: test_source_change_without_an_entry_is_listed
      stops listing a real change, and the audit goes quiet on the very thing it
      exists to catch.
    - A base that does not resolve: test_unresolvable_base_fails_loudly sees a
      zero exit and an empty candidate list, which reads as "nothing missing" for
      a range that was never examined.

The script is run as-is against a purpose-built temporary repository, so the
assertions are about its real behavior on real git history. Committer identity
and signing are passed per invocation with ``git -c`` rather than written to any
configuration, so the tests cannot depend on -- or disturb -- the developer's
git setup.
"""

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "changelog-audit.sh"

# Committed on the first commit so every later commit has a parent, and so the
# audit range never starts at the root commit (which git cannot diff normally).
_BASE_FILE = "seed.txt"

# A path the audit must treat as observable, and one it must not. Kept as
# constants because several tests assert on the same pair.
_SOURCE_FILE = "src/universalchess/services/chess_game.py"
_TEST_FILE = "src/universalchess/tests/test_something.py"

_CHANGELOG = "CHANGELOG.md"

# Headers the audit prints. Candidates are split by whether any changelog commit
# follows them in the range, because those two cases need different action: one is
# owed an entry outright, the other needs a prose check. The trailing list names
# the changelog commits themselves -- a commit there is being reported as a source
# of entries, not accused of missing one, and a whole-output substring check
# cannot tell those two apart.
_UNDESCRIBED_HEADER = "Undescribed"
_POSSIBLY_DESCRIBED_HEADER = "Possibly described"
_CHANGELOG_COMMITS_HEADER = "Changelog commits in the range"


def _candidate_section(stdout):
    """Return the part of the audit output listing candidates of either kind."""
    return stdout.split(_CHANGELOG_COMMITS_HEADER)[0]


def _undescribed_section(stdout):
    """Return only the candidates with no changelog commit after them."""
    if _UNDESCRIBED_HEADER not in stdout:
        return ""
    after_header = stdout.split(_UNDESCRIBED_HEADER, 1)[1]
    return after_header.split(_POSSIBLY_DESCRIBED_HEADER)[0].split(
        _CHANGELOG_COMMITS_HEADER
    )[0]


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

    def commit(self, subject, files):
        """Create a commit touching every path in ``files``."""
        for relative in files:
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a") as handle:
                handle.write(f"{subject}\n")
            self.git("add", relative)
        self.git("commit", "-m", subject)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def audit(self, *args):
        return subprocess.run(  # noqa: S603 - test invokes the repo's own script
            ["bash", str(_SCRIPT), *args],  # noqa: S607
            cwd=str(self.path), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60,
        )


@pytest.fixture
def repo(tmp_path):
    """An initialized repository with one seed commit, and its base ref."""
    fixture = Repo(tmp_path)
    fixture.git("init", "-b", "main")
    fixture.commit("Seed the repository.", [_BASE_FILE])
    return fixture


def test_source_change_without_an_entry_is_listed(repo):
    """The core case: a behavior change that never reached the changelog.

    Regression: the audit reports nothing for a commit that changed shipped code
    without an entry, which is exactly the omission that reached main three times
    and read as "nothing missing".
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE])
    result = repo.audit(base)
    assert "Change how a game is scored." in _candidate_section(result.stdout), result.stdout


def test_listing_names_the_files_that_prompted_it(repo):
    """A candidate is judged by a human, who needs to see what changed.

    Regression: the audit prints subjects only, so deciding whether an entry is
    owed means re-running git for every line and the list gets skimmed instead of
    read.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE, _TEST_FILE])
    result = repo.audit(base)
    assert _SOURCE_FILE in _candidate_section(result.stdout), result.stdout


def test_entry_in_the_same_commit_is_not_listed(repo):
    """One of the two accepted shapes: the entry travels with the change.

    Regression: every properly documented commit is listed too, so the output is
    noise and stops being read -- which is how a gate becomes worthless.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE, _CHANGELOG])
    result = repo.audit(base)
    assert "Change how a game is scored." not in _candidate_section(result.stdout), result.stdout


def test_changelog_only_commit_is_reported_as_covering_work(repo):
    """The other accepted shape: a following "Note ..." commit.

    The audit cannot know which candidates such a commit covers, so it must show
    them rather than silently assume either way. Regression: the "Note ..."
    commit is hidden, and a reader cannot tell an uncovered change from one
    already described in a later commit.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE])
    repo.commit("Note the scoring change in the changelog.", [_CHANGELOG])
    result = repo.audit(base)
    assert "Note the scoring change in the changelog." in result.stdout, result.stdout


@pytest.mark.parametrize(
    "exempt_file",
    [_TEST_FILE, "src/universalchess/MODULE_SUMMARY.md", ".cursor/rules/changelog.mdc"],
    ids=["python-test", "module-summary", "agent-rule"],
)
def test_changes_with_no_observable_effect_are_not_listed(repo, exempt_file):
    """Tests, internal module docs and agent rules change nothing a user sees.

    Regression: these are listed, and because they are frequent the real
    candidates are buried among them.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Add a guard for the scoring rule.", [exempt_file])
    result = repo.audit(base)
    assert "Add a guard for the scoring rule." not in _candidate_section(result.stdout), result.stdout


def test_scripts_are_not_exempt(repo):
    """Developer tooling has its own entries in this changelog.

    `deploy-to-pi.sh` has two, so exempting scripts/ would drop a category the
    project demonstrably documents. Regression: a tooling contract changes and
    the audit stays quiet.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change what the deploy verifies.", ["scripts/deploy-to-pi.sh"])
    result = repo.audit(base)
    assert "Change what the deploy verifies." in _candidate_section(result.stdout), result.stdout


def test_clean_range_reports_no_candidates(repo):
    """The audit must be able to say "nothing owed" and exit zero.

    Guards against over-correcting into always reporting something, which would
    make the audit unusable in a pre-push habit. Regression: a documented range
    still lists candidates.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE, _CHANGELOG])
    result = repo.audit(base)
    assert result.returncode == 0, result.stderr
    assert "no commits" in result.stdout.lower(), result.stdout


def test_unresolvable_base_fails_loudly(repo):
    """An unexamined range must never read as a clean one.

    This is the failure this project keeps correcting: a probe that cannot run
    produces no findings, and no findings is reported as a pass. Regression: a
    typo'd base exits 0 with an empty list and the range is never audited.
    """
    result = repo.audit("no-such-ref")
    assert result.returncode != 0, result.stdout
    assert "no-such-ref" in result.stdout + result.stderr


def test_strict_mode_exits_non_zero_when_entries_are_owed(repo):
    """--strict makes the audit usable as a gate without being one by default.

    Advisory by default because a candidate is a judgement call and a check that
    usually fails gets bypassed. Regression: --strict passes with candidates
    outstanding, so anything wired to it silently stops enforcing.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE])
    strict = repo.audit("--strict", base)
    assert strict.returncode != 0, strict.stdout
    assert repo.audit(base).returncode == 0, "default mode must stay advisory"


class TestCandidatesAreSplitByWhetherAnEntryCanExist:
    """Two kinds of candidate need two different actions, so they are separated.

    Run against this repo's own history, every candidate turned out to be covered
    by a later "Note ... in the changelog" commit. A flat list where each line is
    a false alarm trains the reader to skim it, which is the failure the script's
    own header warns about. Whether a following commit *covers* a candidate is a
    question about prose, but whether one *exists* is mechanical -- and a candidate
    with none is owed an entry outright.
    """

    def test_candidate_with_no_following_changelog_commit_is_called_undescribed(self, repo):
        # Regression: this is merged back into one list, and the only unambiguous
        # finding the audit can make is buried among the ambiguous ones.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change how a game is scored.", [_SOURCE_FILE])
        result = repo.audit(base)
        assert "Change how a game is scored." in _undescribed_section(result.stdout), (
            result.stdout
        )

    def test_candidate_followed_by_a_changelog_commit_is_not_called_undescribed(self, repo):
        # The shape this repo actually uses: the change lands, a "Note ..." commit
        # follows. It is still shown for a prose check, but must not be reported as
        # owed. Regression: correctly documented work is reported as undescribed,
        # and --strict fails on a clean range.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change how a game is scored.", [_SOURCE_FILE])
        repo.commit("Note the scoring change in the changelog.", [_CHANGELOG])
        result = repo.audit(base)
        assert "Change how a game is scored." not in _undescribed_section(result.stdout)
        assert "Change how a game is scored." in _candidate_section(result.stdout)

    def test_strict_ignores_candidates_a_later_commit_may_cover(self, repo):
        # --strict must gate on the mechanical finding only. Regression: strict
        # fails whenever any candidate exists, so wiring it to a hook blocks
        # properly documented work and the hook gets bypassed.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change how a game is scored.", [_SOURCE_FILE])
        repo.commit("Note the scoring change in the changelog.", [_CHANGELOG])
        assert repo.audit("--strict", base).returncode == 0, repo.audit(base).stdout

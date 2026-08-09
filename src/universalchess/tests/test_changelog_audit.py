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
_DECLARED_EXEMPT_HEADER = "Declared exempt"
_CHANGELOG_COMMITS_HEADER = "Changelog commits in the range"

# Trailer a commit uses to state that no entry is owed, making a judgement the
# rule already requires in prose readable by the audit as well.
_EXEMPT_TRAILER = "Changelog: none"


def _candidate_section(stdout):
    """Return the part of the audit output listing candidates of either kind."""
    return stdout.split(_CHANGELOG_COMMITS_HEADER)[0]


def _section(stdout, header, *following_headers):
    """Return the text under ``header``, stopping at any of the later headers."""
    if header not in stdout:
        return ""
    body = stdout.split(header, 1)[1]
    for boundary in (*following_headers, _CHANGELOG_COMMITS_HEADER):
        body = body.split(boundary)[0]
    return body


def _undescribed_section(stdout):
    """Return only the candidates with no changelog commit after them."""
    return _section(
        stdout, _UNDESCRIBED_HEADER, _POSSIBLY_DESCRIBED_HEADER, _DECLARED_EXEMPT_HEADER
    )


def _declared_exempt_section(stdout):
    """Return only the commits that declared no entry is owed."""
    return _section(stdout, _DECLARED_EXEMPT_HEADER)


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


def test_runs_under_the_system_bash(repo):
    """The audit must run on a stock macOS shell, where /bin/bash is 3.2.

    The other tests invoke `bash` from PATH, which here is 5.3, so a bash 4-only
    construct would pass them and then fail for anyone without a newer bash
    installed. A negative array index did exactly that before this test existed.
    The consequence is not subtle -- the script refuses to start -- but a developer
    whose audit does not run stops auditing, which is the omission being fixed.

    Coverage is machine-dependent by nature: this is a real 3.2 check on macOS and
    a redundant re-run on a Linux box where /bin/bash is modern.
    """
    system_bash = Path("/bin/bash")
    if not system_bash.exists():
        pytest.skip("no /bin/bash to check against")
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    repo.commit("Change how a game is scored.", [_SOURCE_FILE])
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(system_bash), str(_SCRIPT), base],
        cwd=str(repo.path), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Change how a game is scored." in _undescribed_section(result.stdout), (
        result.stdout or result.stderr
    )


class TestACommitCanDeclareThatNoEntryIsOwed:
    """`Changelog: none` makes a judgement the audit can read.

    Some changes genuinely owe no entry -- developer process tooling, for one:
    "Make the commit hook run the same analysis as CI" has none, and the changelog
    never mentions the commit hook. The rule already requires that call to be
    justified in the commit body, but prose is only readable by a person, so
    --strict could not be wired to a hook without tripping over legitimate work.
    The trailer records the same decision mechanically.

    It is reported, not suppressed. An exemption nobody sees is how a wrong call
    survives, so declaring one moves a commit out of the gate's way while keeping
    it on the page.
    """

    _EXEMPT_BODY = f"Developer tooling only.\n\n{_EXEMPT_TRAILER} -- no user-visible change"

    def test_declared_commit_is_not_reported_as_undescribed(self, repo):
        # Regression: a commit that states no entry is owed is still demanded one,
        # so the gate cannot be trusted and gets switched off.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change the audit's own output.", [_SOURCE_FILE], body=self._EXEMPT_BODY)
        result = repo.audit(base)
        assert "Change the audit's own output." not in _undescribed_section(result.stdout), (
            result.stdout
        )

    def test_declared_commit_is_still_listed(self, repo):
        # The exemption must stay visible: a silent skip is indistinguishable from
        # the audit not looking. Regression: the commit vanishes from the report and
        # a wrong judgement can never be caught by review.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change the audit's own output.", [_SOURCE_FILE], body=self._EXEMPT_BODY)
        result = repo.audit(base)
        assert "Change the audit's own output." in _declared_exempt_section(result.stdout), (
            result.stdout
        )

    def test_declared_reason_is_shown(self, repo):
        # The trailer may carry a reason, which is the part a reviewer judges.
        # Regression: only the key is recognised and the reason is dropped, leaving
        # a bare exemption to be taken on trust.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change the audit's own output.", [_SOURCE_FILE], body=self._EXEMPT_BODY)
        result = repo.audit(base)
        assert "no user-visible change" in _declared_exempt_section(result.stdout), (
            result.stdout
        )

    def test_strict_passes_when_every_finding_is_declared(self, repo):
        # The point of the trailer: --strict becomes wirable to a hook.
        # Regression: strict still fails and the gate remains unusable.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        repo.commit("Change the audit's own output.", [_SOURCE_FILE], body=self._EXEMPT_BODY)
        strict = repo.audit("--strict", base)
        assert strict.returncode == 0, strict.stdout

    def test_prose_mention_does_not_exempt(self, repo):
        # The declaration must be a real git trailer, not any line that resembles
        # one. A body discussing the changelog -- as a commit explaining an omission
        # naturally does -- must not exempt itself by accident. The phrase sits
        # mid-body with a paragraph after it, so it falls outside the trailer block.
        # Regression: the audit greps instead of parsing, and any commit that talks
        # about the changelog silently escapes the gate.
        base = repo.git("rev-parse", "HEAD").stdout.strip()
        body = (
            f"Considered whether {_EXEMPT_TRAILER} applied here.\n\n"
            "It does not: this changes what a user sees.\n\n"
            "Co-authored-by: Someone <someone@example.invalid>"
        )
        repo.commit("Change how a game is scored.", [_SOURCE_FILE], body=body)
        result = repo.audit(base)
        assert "Change how a game is scored." in _undescribed_section(result.stdout), (
            result.stdout
        )
        assert repo.audit("--strict", base).returncode != 0

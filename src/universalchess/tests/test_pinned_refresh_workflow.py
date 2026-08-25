"""Tests that a generated pin refresh lands with a changelog decision recorded.

Why these tests exist:
    The scheduled refresh commits a change to the vendored wheel closure -- the
    exact library versions installed on a board -- with a bare subject and no
    body. That path is not exempt from the changelog audit, so every refresh it
    proposes arrives Undescribed. The first one to be merged proved the point: it
    reached main, and two later commits that touched CHANGELOG.md for unrelated
    work then reclassified it as "Possibly described", moving it out of --strict's
    way and leaving the omission to be caught only by someone reading prose.

    An entry is not owed for routine version drift, which a board cannot observe.
    What was missing was the *decision*, written where the audit can read it. The
    message is built by a script rather than inline in the workflow so the trailer
    placement can be tested: a `Changelog:` line separated from the last paragraph
    is not a trailer at all, and the only symptom is the commit staying flagged
    with no explanation.

How a regression manifests:
    - The trailer stops being a trailer (reordered, or another paragraph appended
      after it): test_generated_commit_is_declared_rather_than_undescribed sees the
      commit back in the Undescribed group, and --strict fails on a clean range.
    - The workflow stops using the generated message: the wiring test fails while
      the message tests still pass, which is the combination that would otherwise
      ship a perfect message nothing uses.
    - The pin list is dropped: reviewers lose the only record of what moved, since
      the diff of a hash-pinned lock is unreadable at a glance.
"""

import subprocess
from pathlib import Path

import pytest

from universalchess.tests.changelog_audit_harness import (
    EXEMPT_TRAILER,
    Repo,
    candidate_section,
    declared_exempt_section,
    initialized_repo,
    undescribed_section,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MESSAGE_SCRIPT = REPO_ROOT / "scripts" / "pinned-refresh-commit-message.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "refresh-pinned-requirements.yml"

# The lock the refresh rewrites, relative to the repository root.
PINNED_LOCK = "src/universalchess/setup/pinned/requirements.txt"

# The subject the workflow commits under, and the one a reader looks for in the
# audit report.
SUBJECT = "Refresh the pinned Python closure"

# A real refresh diff: the webencodings bump that exposed this gap. Kept verbatim,
# trailing backslashes and all, because those continuations are what a naive
# interpolation into a shell heredoc would swallow -- joining the lines and losing
# the versions the message exists to report.
SAMPLE_DIFF = """\
diff --git a/src/universalchess/setup/pinned/requirements.txt \
b/src/universalchess/setup/pinned/requirements.txt
index ae4d7995..b7308eec 100644
--- a/src/universalchess/setup/pinned/requirements.txt
+++ b/src/universalchess/setup/pinned/requirements.txt
@@ -45,5 +45,5 @@ six==1.17.0 \\
     --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274
 tinycss2==1.5.1 \\
     --hash=sha256:3415ba0f5839c062696996998176c4a3751d18b7edaaeeb658c9ce21ec150661
-webencodings==0.5.1 \\
-    --hash=sha256:a0af1213f3c2226497a97e2b3aa01a7e4bee4f403f95be16fc9acd2947514a78
+webencodings==0.6.1 \\
+    --hash=sha256:7fab6269c8bf237c657876b52058ccb182e861518d1c695c1a9aaa8c1c105d5b
"""

# A diff that touched the file without moving a pin -- only the generated header
# comment changed. The script must refuse this rather than announce pins it cannot
# name.
NO_PINS_REFUSAL = "no pins moved"

HEADER_ONLY_DIFF = """\
diff --git a/src/universalchess/setup/pinned/requirements.txt \
b/src/universalchess/setup/pinned/requirements.txt
index ae4d7995..b7308eec 100644
--- a/src/universalchess/setup/pinned/requirements.txt
+++ b/src/universalchess/setup/pinned/requirements.txt
@@ -1,4 +1,4 @@
-# Python wheels vendored into the .deb. GENERATED -- do not edit by hand.
+# Python wheels vendored into the .deb. Generated -- do not edit by hand.
 #
"""


def build_message(diff):
    """Run the message builder over ``diff`` and return the completed process."""
    return subprocess.run(  # noqa: S603 - test invokes the repo's own script
        ["bash", str(MESSAGE_SCRIPT)],  # noqa: S607
        input=diff, capture_output=True, text=True, timeout=60, check=False,
    )


@pytest.fixture
def message():
    """The commit message the workflow would produce for the sample refresh."""
    result = build_message(SAMPLE_DIFF)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    """An initialized repository with one seed commit."""
    return initialized_repo(tmp_path)


def commit_refresh(repo: Repo, message: str) -> str:
    """Commit a lock change in ``repo`` using ``message``, and return the base ref.

    The message is written to a file and passed with ``-F``, exactly as the
    workflow does, so paragraph structure reaches git unaltered. Building it with
    repeated ``-m`` would insert blank lines of its own and could make a trailer
    out of a message that has none.
    """
    base = repo.git("rev-parse", "HEAD").stdout.strip()
    target = repo.path / PINNED_LOCK
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("webencodings==0.6.1\n")
    repo.git("add", PINNED_LOCK)
    message_file = repo.path / "commit-message.txt"
    message_file.write_text(message)
    repo.git("commit", "-F", str(message_file))
    message_file.unlink()
    return base


def test_message_leads_with_the_subject(message):
    """The subject is what the audit report and the log show.

    Regression: the subject is lost into the body, so every refresh commit reads
    as an untitled change and the report lines become unidentifiable.
    """
    assert message.splitlines()[0] == SUBJECT, message


def test_message_names_every_pin_that_moved(message):
    """Both sides of the move must be readable in the body.

    A hash-pinned lock diffs into version-and-digest pairs, so the log alone does
    not say what changed; the body is the only place a reviewer or a reader of the
    release notes can see it. Both the old and the new version are asserted
    because reporting only the new one hides what it replaced.

    Regression: the continuation backslashes in the lock are treated as shell line
    continuations and the version lines are joined or dropped, leaving a message
    that announces pins it does not name.
    """
    assert "webencodings==0.5.1" in message, message
    assert "webencodings==0.6.1" in message, message
    # The lock's line continuations are an artifact of --hash syntax and mean
    # nothing in a commit message; carrying them through is how they get
    # interpreted somewhere they do matter.
    assert "\\" not in message, message
    # Pins that did not move must not be listed as though they had.
    assert "tinycss2" not in message, message
    assert "six" not in message, message


def test_message_ends_with_the_changelog_declaration(message):
    """The trailer has to be the last paragraph or git does not parse it.

    Regression: a paragraph is appended after the declaration -- the natural way
    to add a note later -- and the trailer silently stops counting while still
    reading like one to a human.
    """
    paragraphs = [p for p in message.strip().split("\n\n") if p.strip()]
    assert paragraphs[-1].startswith(EXEMPT_TRAILER), message


def test_message_states_when_the_declaration_would_be_wrong(message):
    """The exemption is a default, and the body must say what overrides it.

    A refresh that carries a security fix *is* owed an entry, and the script
    cannot tell one from routine drift. Recording the condition is what lets a
    reviewer make the call the script cannot.

    Regression: the reason shrinks to a bare "none", and the next reviewer has no
    way to know the exemption was conditional.
    """
    assert "advisory" in message, message


def test_a_diff_that_moved_no_pins_is_refused():
    """The null case: no pins moved, so there is no message to write.

    Reached if the generated header changes without any version changing. A
    message listing nothing under "Pins that moved" would be a lie the log keeps,
    so the script fails and lets the workflow stop instead.

    Regression: an empty pin list is emitted as a valid message, and a refresh
    commit claims a change it cannot name.

    The refusal is matched on a whole phrase rather than the word "pin": the
    script's own name contains it, so a "No such file" error satisfies the looser
    check and the test passes while nothing runs.
    """
    result = build_message(HEADER_ONLY_DIFF)
    assert result.returncode != 0, result.stdout
    assert not result.stdout.strip(), result.stdout
    assert NO_PINS_REFUSAL in result.stderr.lower(), result.stderr


def test_generated_commit_is_declared_rather_than_undescribed(repo, message):
    """The whole point: the audit must read the decision the script recorded.

    This is the end-to-end assertion -- the real message, a real commit touching
    the real lock path, the real audit script. Regression: the refresh is back in
    the Undescribed group, which is the state that let the webencodings bump reach
    main unexplained.
    """
    base = commit_refresh(repo, message)
    result = repo.audit(base)
    assert SUBJECT not in undescribed_section(result.stdout), result.stdout
    assert SUBJECT in declared_exempt_section(result.stdout), result.stdout


def test_generated_commit_stays_visible_in_the_report(repo, message):
    """A declared exemption is reported, never hidden.

    The audit shows exemptions so a wrong call can still be caught. Regression:
    the refresh disappears from the report entirely, and a bump that genuinely
    needed an entry becomes invisible rather than merely unflagged.
    """
    base = commit_refresh(repo, message)
    result = repo.audit(base)
    assert SUBJECT in candidate_section(result.stdout), result.stdout
    assert "advisory" in declared_exempt_section(result.stdout), result.stdout


def test_strict_audit_passes_for_a_generated_refresh_commit(repo, message):
    """--strict must not trip on a refresh that declared its decision.

    Regression: --strict fails on every weekly refresh, so anything wired to it
    gets bypassed -- the failure mode the audit's own header warns about.
    """
    base = commit_refresh(repo, message)
    strict = repo.audit("--strict", base)
    assert strict.returncode == 0, strict.stdout


def test_the_workflow_commits_with_the_generated_message():
    """The workflow must actually use the script.

    Why this test exists: every other assertion here is about the script's output.
    If the workflow keeps committing with a bare `-m` subject, all of them pass
    and nothing changes on main. Nothing else connects the two.

    How a regression manifests: refreshes arrive Undescribed again, while the
    suite reports the message machinery as working.
    """
    workflow = WORKFLOW.read_text()
    assert MESSAGE_SCRIPT.name in workflow, (
        f"{WORKFLOW.name} does not call {MESSAGE_SCRIPT.name}, so the changelog "
        "declaration never reaches the commit it is written for"
    )
    assert "git commit -F" in workflow, (
        "the refresh must commit with a message file; a bare -m subject drops the "
        "body and with it the trailer"
    )

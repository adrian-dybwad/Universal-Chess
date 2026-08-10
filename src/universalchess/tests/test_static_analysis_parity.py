"""Tests that the pre-commit gate runs what the CI gate runs.

A commit hook exists to say, before pushing, whether CI will pass. When the two
run different rules, it says so incorrectly, and the cost is paid at the worst
moment: after the push, with a red branch, by someone who already believed the
work was checked. That is worse than having no hook, because a hook that passes
is taken as evidence.

The divergence this guards against was real. The hook ran semgrep with
``SEMGREP_OFFLINE=1``, which restricts it to the handful of local rules in
``.semgrep/``, while CI ran those plus five registry packs -- 405 rules against
3. It also pinned its own ruff and bandit versions, independent of the ones
``requirements-dev.txt`` installs for CI. Both were deliberate, for speed and
convenience, and both silently narrowed what a green commit meant.

Parity is asserted structurally: both sides must invoke ``scripts/analyze.sh``,
which is the single definition of the toolchain, rather than each spelling out
tools and configs that then drift apart.

The frontend has the same arrangement and the same requirement. Its gate ran a
security-only eslint config while ``npm run lint`` ran the full ruleset to a
backlog of 44 findings, so the two sides again checked different things. There
is now one script, ``npm run lint``, gated in both places.
"""

import json
import re
from pathlib import Path

import yaml

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "static-analysis.yml"
WEB_APP = PACKAGE_ROOT / "web-app"
ANALYZE = "scripts/analyze.sh"
LINT_SCRIPT = "npm run lint"
# `npm run lint`, tolerating flags npm itself takes (e.g. --silent in the hook).
LINT_INVOCATION = re.compile(r"npm run (?:--\S+ )*lint\b(?!:)")

# Third-party hook repositories that would supply their own copy of a tool the
# runner already provides, at a version pinned separately from requirements-dev.
COMPETING_TOOL_REPOS = ("ruff-pre-commit", "bandit", "semgrep")


def _hooks():
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            yield repo.get("repo", ""), hook


def _python_analysis_hooks():
    """Hooks that gate Python static analysis by calling the shared runner."""
    return [hook for _, hook in _hooks() if ANALYZE in hook.get("entry", "")]


def test_pre_commit_runs_semgrep_with_the_registry_packs():
    """No hook may restrict semgrep to local rules only.

    Why this test exists: ``SEMGREP_OFFLINE=1`` cuts the rule set from 405 to 3.
    A commit passed the hook and then failed CI on a finding the hook was never
    capable of detecting, which is the exact failure this module exists to
    prevent.

    How a regression manifests: commits pass locally and static analysis fails on
    push, with a finding that cannot be reproduced by running the hook again --
    the most confusing shape this bug can take.
    """
    offenders = [
        line.strip()
        for line in PRE_COMMIT_CONFIG.read_text().splitlines()
        if "SEMGREP_OFFLINE" in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        "the pre-commit gate must run the same semgrep rules as CI; setting "
        f"SEMGREP_OFFLINE makes a passing commit meaningless: {offenders}"
    )


def test_pre_commit_and_ci_invoke_the_same_runner():
    """Both gates must go through ``scripts/analyze.sh``.

    Why this test exists: the runner defines the tools, their configs and the
    registry packs in one place. A hook that invokes ruff, bandit or semgrep
    directly re-states that definition, and the copy is what drifts -- silently,
    since both sides keep passing while checking different things.

    How a regression manifests: a config change lands in one place only, and the
    hook's verdict stops predicting CI's, without any error to indicate it.
    """
    assert _python_analysis_hooks(), (
        f"no pre-commit hook invokes {ANALYZE}; the Python gate must use the same "
        "runner as CI rather than configuring the tools separately"
    )
    assert ANALYZE in CI_WORKFLOW.read_text(), (
        f"the CI gate must invoke {ANALYZE} so both sides share one definition"
    )


def test_pre_commit_scopes_analysis_to_the_files_being_committed():
    """The hook must pass its files through ``ANALYZE_FILES``, as CI does.

    Why this test exists: the runner treats an unset ``ANALYZE_FILES`` as "scan
    the whole tree", which fails on the large pre-existing backlog and would make
    every commit impossible. CI sets it to the changed files so the gate covers
    new findings and findings in files being touched. The hook has to scope the
    same way, or it is testing a different thing again -- just in the other
    direction.

    How a regression manifests: either every commit is blocked by unrelated
    legacy findings, or, if the variable is set but empty, nothing is scanned at
    all and the gate passes unconditionally.
    """
    unscoped = [
        hook["id"]
        for hook in _python_analysis_hooks()
        if "ANALYZE_FILES" not in hook.get("entry", "")
    ]
    assert not unscoped, (
        "hooks calling the runner must set ANALYZE_FILES from the files being "
        f"committed, matching how CI scopes the gate: {unscoped}"
    )

    unfed = [
        hook["id"]
        for hook in _python_analysis_hooks()
        if hook.get("pass_filenames") is False
    ]
    assert not unfed, (
        "hooks calling the runner must receive the committed filenames; with "
        f"pass_filenames disabled there is nothing to put in ANALYZE_FILES: {unfed}"
    )


def test_no_hook_supplies_its_own_copy_of_a_runner_tool():
    """ruff, bandit and semgrep must come from one pinned set of versions.

    Why this test exists: a third-party hook repository pins its own tool version
    in ``.pre-commit-config.yaml``, while CI installs from
    ``requirements-dev.txt``. Two pins for one tool drift apart on their own
    schedules, and different versions of a linter disagree about findings, so the
    hook and CI reach different verdicts while both appear correctly configured.

    How a regression manifests: a rule added in a newer ruff or bandit fires in
    CI and not locally, or the reverse, with no indication that two versions are
    involved.
    """
    offenders = [
        f"{repo} -> {hook.get('id')}"
        for repo, hook in _hooks()
        if any(name in repo for name in COMPETING_TOOL_REPOS)
    ]
    assert not offenders, (
        "these hooks install their own ruff/bandit/semgrep, pinned separately "
        "from requirements-dev.txt which CI uses; route them through "
        f"{ANALYZE} instead so one set of versions applies: {offenders}"
    )


def _eslint_hook_entries():
    """Pre-commit entries that run eslint, however they spell it."""
    return [
        hook.get("entry", "")
        for _, hook in _hooks()
        if "eslint" in hook.get("entry", "") or "lint" in hook.get("id", "")
    ]


def test_both_gates_run_the_same_frontend_lint():
    """The hook and CI must run one eslint command, not two rulesets.

    Why this test exists: the frontend repeated the Python mistake in a different
    shape. The gate ran ``lint:security``, a config carrying only the XSS rules,
    while ``npm run lint`` -- the full ruleset, including every react-hooks rule
    -- was left ungated because it had a backlog. A green commit therefore said
    nothing about the rules that reported 44 findings, and there was no moment at
    which anyone was told.

    How a regression manifests: a second lint script appears "just for the gate",
    the two rulesets drift, and the hook stops predicting CI again.
    """
    hook_entries = [entry for entry in _eslint_hook_entries() if LINT_INVOCATION.search(entry)]
    assert hook_entries, (
        f"a pre-commit hook must run `{LINT_SCRIPT}`; a narrower script would gate "
        "fewer rules than CI"
    )
    narrower = [
        entry
        for entry in _eslint_hook_entries()
        if "lint:" in entry  # e.g. the retired `lint:security`
    ]
    assert not narrower, (
        "these hooks run a lint script other than the full one, which is how the "
        f"frontend gate came to check less than CI: {narrower}"
    )
    assert LINT_INVOCATION.search(CI_WORKFLOW.read_text()), (
        f"the CI frontend gate must run `{LINT_SCRIPT}`, the same script the hook runs"
    )


def test_the_frontend_lint_fails_on_warnings():
    """``npm run lint`` must treat a warning as a failure.

    Why this test exists: eslint exits 0 on warnings, so a gate without
    ``--max-warnings 0`` passes while reporting them. The warnings that matter
    here are unused ``eslint-disable`` directives: three suppressions are in the
    tree for effects that load once on mount, and each is only honest while the
    finding it names is still reported. Without this flag a stale suppression is
    invisible and accumulates.

    How a regression manifests: disables outlive their findings, the tree looks
    clean, and the next person reads suppressions that no longer describe
    anything.
    """
    scripts = json.loads((WEB_APP / "package.json").read_text())["scripts"]
    assert "--max-warnings 0" in scripts["lint"], (
        "the lint script must fail on warnings, or a stale eslint-disable stays "
        f"green forever: {scripts['lint']}"
    )


def test_requirements_dev_pins_the_tools_both_gates_rely_on():
    """The shared version pin must actually exist.

    Why this test exists: the test above removes the hooks' own pins on the
    grounds that ``requirements-dev.txt`` is the single source. That reasoning
    only holds while the file really pins these tools; an unpinned entry would
    install whatever is current, so the local venv and the CI runner could still
    end up on different versions.

    How a regression manifests: the same divergence as separate pins, now with
    nothing in either config to suggest versions are in play.
    """
    text = (REPO_ROOT / "requirements-dev.txt").read_text()
    unpinned = [
        tool
        for tool in ("ruff", "bandit", "semgrep")
        if not re.search(rf"^{tool}\s*==", text, re.MULTILINE | re.IGNORECASE)
    ]
    assert not unpinned, (
        "requirements-dev.txt must pin these to an exact version, since both the "
        f"pre-commit gate and CI take their tools from it: {unpinned}"
    )

"""Tests that pinned Python dependencies still receive security updates.

Pinning the vendored wheels to exact versions and hashes is what stops root from
running whatever PyPI serves at install time. It also freezes those versions: a
patched CVE no longer arrives on its own, the way it did when every install
resolved afresh. Two separate mechanisms replace what pinning removed, and they
answer different questions.

*Being told* a pin is vulnerable depends on GitHub's dependency graph parsing the
file. That parsing matches manifests by exact filename -- ``requirements.txt``
for pip -- and skips paths that look like vendored third-party code. A lock named
anything else produces no Dependabot alerts at all, silently, which is why the
path is asserted here rather than left to convention.

*Fixing* it depends on regenerating the whole closure. Dependabot's own pip
updater re-pins the one distribution it targets and leaves that package's
transitives at their old versions and hashes, which breaks a
``--require-hashes`` install. The refresh workflow runs the resolver that
produces a coherent lock, so the alert has a working remedy behind it.
"""

import re
from pathlib import Path

import yaml

import universalchess

PACKAGE_ROOT = Path(universalchess.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
PINNED_REQUIREMENTS = PACKAGE_ROOT / "setup" / "pinned" / "requirements.txt"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
REFRESH_WORKFLOW = WORKFLOWS_DIR / "refresh-pinned-requirements.yml"
LOCK_GENERATOR = REPO_ROOT / "scripts" / "update-wheels-lock.py"

# Directory names GitHub treats as vendored third-party code and excludes from
# the dependency graph. Transcribed from the dependency graph documentation; a
# manifest under any of these is parsed by nothing and alerts on nothing.
VENDORED_DIRECTORY_PATTERNS = (
    r"(3rd|[Tt]hird)[-_]?[Pp]arty/",
    r"(^|/)vendors?/",
    r"(^|/)[Ee]xtern(als?)?/",
    r"(^|/)[Vv]+endor/",
)


def _repo_relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _dependabot() -> dict:
    return yaml.safe_load(DEPENDABOT_CONFIG.read_text())


def _pip_directories() -> set:
    """Every directory the pip ecosystem is configured to watch.

    Handles both spellings dependabot accepts, ``directory`` and ``directories``,
    so tightening the config to one entry does not read as a missing entry.
    """
    watched = set()
    for update in _dependabot().get("updates", []):
        if update.get("package-ecosystem") != "pip":
            continue
        if "directory" in update:
            watched.add(update["directory"])
        watched.update(update.get("directories", []))
    return watched


def test_pinned_requirements_use_the_filename_the_dependency_graph_parses():
    """The lock must be named exactly ``requirements.txt``.

    Why this test exists: GitHub identifies pip manifests by exact filename and
    does not glob. The file was previously ``wheels.lock``, which the dependency
    graph ignored entirely -- the pinned distributions produced no Dependabot
    alerts, so a published CVE in any of them would have gone unreported for as
    long as nobody happened to read the news.

    How a regression manifests: invisibly and only in the security posture.
    Renaming the file back, or moving it somewhere tidier, keeps every build and
    install working exactly as before while quietly switching vulnerability
    alerting off for the whole vendored closure.
    """
    assert PINNED_REQUIREMENTS.exists(), (
        f"{_repo_relative(PINNED_REQUIREMENTS)} is missing; the vendored pins must "
        "live in a file the dependency graph parses"
    )
    assert PINNED_REQUIREMENTS.name == "requirements.txt", (
        "the pinned lock must be named requirements.txt exactly: GitHub matches "
        f"pip manifests by filename, not by pattern, so {PINNED_REQUIREMENTS.name} "
        "would be ignored by the dependency graph and raise no alerts"
    )


def test_pinned_requirements_are_not_under_a_path_github_treats_as_vendored():
    """The lock's path must not match GitHub's vendored-directory patterns.

    Why this test exists: the dependency graph skips manifests under directories
    named ``vendor``, ``third-party``, ``external`` and similar. Those names are
    the obvious ones to reach for when housing vendored wheels, and choosing one
    would disable alerting just as completely as the wrong filename, while
    looking more correct than the name that works.

    How a regression manifests: the same silent loss of alerts as a rename, with
    nothing in any build or test output to indicate it.
    """
    relative = _repo_relative(PINNED_REQUIREMENTS)
    offenders = [
        pattern
        for pattern in VENDORED_DIRECTORY_PATTERNS
        if re.search(pattern, relative)
    ]
    assert not offenders, (
        f"{relative} sits under a directory GitHub excludes as vendored code "
        f"(matched {offenders}); the dependency graph will not parse it and no "
        "alerts will be raised for the pinned distributions"
    )


def test_dependabot_watches_the_directory_holding_the_pinned_requirements():
    """Dependabot's pip ecosystem must cover the pinned lock's directory.

    Why this test exists: a correctly named manifest in an unwatched directory
    still yields no version-update pull requests. The alerting half and the
    update half are configured separately, and having only one of them is the
    failure mode that looks fine from either side on its own.

    How a regression manifests: pins quietly stop being offered updates. Nothing
    fails; the repository simply stops hearing about newer versions, so the
    closure ages until an install breaks or a CVE is found by other means.
    """
    expected = "/" + _repo_relative(PINNED_REQUIREMENTS.parent)
    watched = _pip_directories()
    assert expected in watched, (
        f"dependabot.yml must watch {expected} under package-ecosystem: pip so the "
        f"pinned lock receives update pull requests; it currently watches {sorted(watched)}"
    )


def test_a_workflow_regenerates_the_whole_pinned_closure():
    """A workflow must run the resolver, on a schedule and on demand.

    Why this test exists: Dependabot bumps the distribution it targets and
    re-hashes only that entry, leaving that package's transitives pinned to their
    previous versions. The result fails ``pip install --require-hashes``, so an
    alert on its own does not come with a fix that can be merged. Regenerating
    the closure with the resolver is what turns an alert into a working update,
    and a manual trigger is what makes a same-day response to a critical advisory
    possible without waiting for the schedule.

    How a regression manifests: alerts keep arriving and every proposed fix fails
    the offline resolution check, so updates stall indefinitely while appearing
    to be handled.
    """
    assert REFRESH_WORKFLOW.exists(), (
        f"{_repo_relative(REFRESH_WORKFLOW)} is missing; pinned versions would only "
        "ever change when somebody remembers to run the resolver by hand"
    )
    workflow = yaml.safe_load(REFRESH_WORKFLOW.read_text())

    # PyYAML resolves the unquoted YAML 1.1 key `on` to the boolean True, which is
    # why the trigger block is looked up under both spellings.
    triggers = workflow.get("on", workflow.get(True, {}))
    assert "workflow_dispatch" in triggers, (
        "the refresh workflow must be manually dispatchable so a critical advisory "
        "can be answered without waiting for the next scheduled run"
    )
    assert "schedule" in triggers, (
        "the refresh workflow must run on a schedule so pins do not age silently "
        "between advisories"
    )

    body = REFRESH_WORKFLOW.read_text()
    generator = _repo_relative(LOCK_GENERATOR)
    assert generator in body, (
        f"the refresh workflow must run {generator}, the only thing that produces a "
        "coherent closure; re-pinning entries individually breaks --require-hashes"
    )
    assert "--verify" in body, (
        "the refresh workflow must resolve the regenerated lock offline before "
        "opening a pull request, so a closure that cannot install is caught here "
        "rather than by whoever reviews the bump"
    )


def test_workflows_running_the_resolver_pin_the_interpreter_it_requires():
    """Any workflow invoking the resolver must set up the Python it demands.

    Why this test exists: pip evaluates environment markers against the running
    interpreter, so the closure depends on which Python resolves it. The script
    enforces one version, but a workflow that does not install it either fails
    outright or, worse, would resolve a closure that installs on trixie and not
    on bookworm. The runner's default Python drifts on its own schedule, so
    leaving the two to agree by coincidence is what this prevents.

    How a regression manifests: adding a workflow step that calls the resolver
    without a matching setup-python fails the job with the interpreter guard's
    message -- recognisable, but only after someone waits for CI. If the guard is
    ever relaxed, the failure moves to a bookworm board instead.
    """
    match = re.search(
        r"^RESOLUTION_PYTHON\s*=\s*\((\d+),\s*(\d+)\)",
        LOCK_GENERATOR.read_text(),
        re.MULTILINE,
    )
    assert match, (
        f"{_repo_relative(LOCK_GENERATOR)} must declare RESOLUTION_PYTHON; the "
        "workflows are checked against it"
    )
    required = f"{match.group(1)}.{match.group(2)}"

    unpinned = []
    for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
        body = workflow.read_text()
        if LOCK_GENERATOR.name not in body:
            continue
        if f"python-version: '{required}'" not in body:
            unpinned.append(_repo_relative(workflow))
    assert not unpinned, (
        f"these workflows run the resolver without setting up Python {required}, "
        f"which it requires: {unpinned}"
    )

"""Tests for the pure ref-selection and file-glob helpers used by installs.

These helpers decide, from a user-requested ref, which ref to build/record and
whether the prebuilt archive may be used, and they expand ``extra_files`` glob
patterns. They are pure so the install flow's branching can be guarded
deterministically without compiling an engine or hitting the network.
"""

from universalchess.managers.engine_manager import (
    EngineDefinition,
    canonical_ref,
    git_ref_for_label,
    merge_ref_list,
    parse_github_repo,
    prebuilt_allowed_for_ref,
    resolve_requested_ref,
    expand_extra_files,
)
from universalchess.services.engine_install_record import DEFAULT_REF


def _engine(git_ref=None, repo_url="https://github.com/owner/repo.git", has_prebuilt=True):
    """Minimal source-build engine definition for ref-logic tests."""
    return EngineDefinition(
        name="x", display_name="X", summary="", description="",
        repo_url=repo_url, build_commands=[], binary_path="x",
        is_system_package=False, package_name=None, extra_files=[], dependencies=[],
        git_ref=git_ref, has_prebuilt=has_prebuilt,
    )


class TestCanonicalRef:
    def test_pinned_engine_canonical_is_the_pin(self):
        """A pinned engine's canonical ref is its catalog pin.

        Why: the canonical ref is what the prebuilt archive represents and what an
        unspecified install resolves to. For a pinned engine that is the pin.
        Manifests: returning DEFAULT_REF here would make the prebuilt look stale
        for the pin and force needless source builds.
        """
        assert canonical_ref(_engine(git_ref="v25.4")) == "v25.4"

    def test_unpinned_engine_canonical_is_default_branch(self):
        """An unpinned engine's canonical ref is the default-branch sentinel.

        Why: unpinned engines build from the default branch; that is the ref the
        prebuilt represents. Manifests: returning None would break the equality
        checks the prebuilt/record logic relies on.
        """
        assert canonical_ref(_engine(git_ref=None)) == DEFAULT_REF


class TestResolveRequestedRef:
    def test_none_resolves_to_canonical(self):
        """An unspecified request resolves to the canonical ref (legacy clients).

        Why: clients that do not send a ref must behave exactly as before -- build
        and record the canonical ref. Manifests: a regression returning DEFAULT_REF
        for a pinned engine would record the wrong ref as installed.
        """
        assert resolve_requested_ref(_engine(git_ref="v25.4"), None) == "v25.4"
        assert resolve_requested_ref(_engine(git_ref=None), None) == DEFAULT_REF

    def test_explicit_ref_is_used_verbatim(self):
        """An explicit ref is the one built and recorded, overriding the pin.

        Why: the whole feature is trying a different release; the requested tag
        must win. Manifests: returning the pin instead would ignore the user's
        selection.
        """
        assert resolve_requested_ref(_engine(git_ref="v25.4"), "v25.5") == "v25.5"


class TestPrebuiltAllowedForRef:
    def test_prebuilt_allowed_for_none_and_canonical(self):
        """The prebuilt is usable only for an unspecified or canonical ref.

        Why: prebuilt archives are built solely from the canonical ref, so they may
        only satisfy that exact request. Manifests: allowing prebuilt for an
        arbitrary tag would install the canonical binary while claiming the
        requested tag.
        """
        engine = _engine(git_ref="v25.4")
        assert prebuilt_allowed_for_ref(engine, None) is True
        assert prebuilt_allowed_for_ref(engine, "v25.4") is True

    def test_prebuilt_blocked_for_other_ref(self):
        """A non-canonical ref must force a source build (no prebuilt).

        Why: there is no prebuilt for v25.5, so using the v25.4 archive would be a
        lie. Manifests: a True here would silently install the wrong version.
        """
        assert prebuilt_allowed_for_ref(_engine(git_ref="v25.4"), "v25.5") is False

    def test_default_sentinel_allows_prebuilt_for_unpinned(self):
        """Selecting the default branch on an unpinned engine uses its prebuilt.

        Why: for an unpinned engine the prebuilt IS the default branch build, so
        explicitly choosing default should still hit the fast path. Manifests:
        blocking it would force a needless source build for the common case.
        """
        assert prebuilt_allowed_for_ref(_engine(git_ref=None), DEFAULT_REF) is True


class TestGitRefForLabel:
    def test_default_sentinel_maps_to_none(self):
        """The default sentinel maps to None so the clone omits --branch.

        Why: a default-branch build is an unpinned clone; the clone code keys off
        None to skip --branch. Manifests: passing the literal "default" to git would
        try to clone a branch named "default" and fail.
        """
        assert git_ref_for_label(DEFAULT_REF) is None

    def test_tag_label_maps_to_itself(self):
        """A tag label is the git ref to clone unchanged.

        Why: a real tag must be passed through to ``git clone --branch``.
        Manifests: any transformation here would clone the wrong ref.
        """
        assert git_ref_for_label("v25.5") == "v25.5"


class TestParseGithubRepo:
    def test_parses_owner_and_repo_stripping_git_suffix(self):
        """owner/repo are parsed from a .git HTTPS URL, suffix stripped.

        Why: the refs endpoint builds the GitHub tags API URL from owner/repo.
        Manifests: keeping the .git suffix would 404 the tags request.
        """
        parsed = parse_github_repo("https://github.com/jdart1/arasan-chess.git")
        assert parsed == ("jdart1", "arasan-chess")

    def test_parses_without_git_suffix(self):
        """A URL without the .git suffix parses the same.

        Why: not all repo_urls carry .git. Manifests: requiring the suffix would
        return None for valid URLs.
        """
        assert parse_github_repo("https://github.com/owner/repo") == ("owner", "repo")

    def test_non_github_or_missing_returns_none(self):
        """Non-GitHub or empty URLs yield None (no tags discovery possible).

        Why: only GitHub tag discovery is implemented; other hosts/None must
        degrade gracefully rather than crash. Manifests: a crash here would break
        the refs endpoint for bundled or non-GitHub engines.
        """
        assert parse_github_repo(None) is None
        assert parse_github_repo("https://gitlab.com/owner/repo.git") is None


class TestMergeRefList:
    def test_merges_marks_and_orders(self):
        """Catalog pin, installed, working history and tags merge with flags.

        Why: the picker needs a single de-duplicated list where each ref carries
        its known-working / pin / installed flags, with the recommended ref first.
        Manifests: a duplicate (a tag also in history) or a mislabeled flag would
        show the same release twice or fail to mark a verified release.
        """
        result = merge_ref_list(
            recommended="v25.4",
            installed_ref="v25.4",
            pin="v25.4",
            working_refs=["v25.4", "v25.3"],
            tags=["v25.5", "v25.4", "v25.3"],
            default_branch="master",
        )
        refs = [r["ref"] for r in result]
        # No duplicates.
        assert len(refs) == len(set(refs))
        # Recommended ref is first.
        assert refs[0] == "v25.4"
        # The default branch is offered as a branch entry whose value is the
        # DEFAULT_REF sentinel (an unpinned clone) but whose label is the real
        # branch name, so selecting it installs the default branch.
        assert any(
            r["ref"] == DEFAULT_REF and r["kind"] == "branch" and r["label"] == "master"
            for r in result
        )

        by_ref = {r["ref"]: r for r in result}
        # v25.4: pin + installed + known-working (it is in working history).
        assert by_ref["v25.4"]["is_pin"] is True
        assert by_ref["v25.4"]["installed"] is True
        assert by_ref["v25.4"]["known_working"] is True
        # v25.3 worked before but is not installed nor the pin.
        assert by_ref["v25.3"]["known_working"] is True
        assert by_ref["v25.3"]["installed"] is False
        assert by_ref["v25.3"]["is_pin"] is False
        # v25.5 is a fresh tag: present but not yet known-working.
        assert by_ref["v25.5"]["known_working"] is False

    def test_history_ref_absent_from_tags_is_still_listed(self):
        """A working ref that GitHub no longer returns is still offered.

        Why: "ever worked in the past" must remain selectable even if the tag fell
        out of the capped GitHub tag window. Manifests: dropping it would hide a
        known-good release the user previously ran.
        """
        result = merge_ref_list(
            recommended=DEFAULT_REF, installed_ref=None, pin=None,
            working_refs=["v1.0"], tags=["v3.0", "v2.0"], default_branch="main",
        )
        assert any(r["ref"] == "v1.0" and r["known_working"] for r in result)


class TestExpandExtraFiles:
    def test_literal_entries_resolve_relative_to_base(self, tmp_path):
        """A non-glob entry resolves to base/entry when it exists.

        Why: existing engines list literal files/dirs (personalities, books); these
        must keep resolving exactly as before. Manifests: a regression would skip
        real extra files and install an incomplete engine.
        """
        (tmp_path / "books").mkdir()
        (tmp_path / "weights.bin").write_text("x")
        result = expand_extra_files(tmp_path, ["books", "weights.bin", "missing"])
        names = sorted(p.name for p in result)
        # "missing" does not exist, so it is omitted.
        assert names == ["books", "weights.bin"]

    def test_glob_entry_matches_multiple(self, tmp_path):
        """A glob entry expands to every match in the base dir.

        Why: Arasan's NNUE filename is version-specific; a "*.nnue" glob lets any
        tag's network install without hardcoding the name. Manifests: if globs were
        treated literally, base/"*.nnue" would not exist and the network would be
        skipped, breaking the engine at runtime.
        """
        (tmp_path / "netA.nnue").write_text("a")
        (tmp_path / "netB.nnue").write_text("b")
        (tmp_path / "readme.txt").write_text("x")
        result = expand_extra_files(tmp_path, ["*.nnue"])
        names = sorted(p.name for p in result)
        assert names == ["netA.nnue", "netB.nnue"]

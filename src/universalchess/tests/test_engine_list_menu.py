"""The board's engine list, rendered from the shared view-model.

Background / why these tests exist
----------------------------------
This screen had no test at all, which is how it drifted from the web until the
two showed the same catalog differently. It built its own list: sorted
installed-first then alphabetically, with no strength groups, no architecture
check, no custom engines and no repair state.

It now renders the rows the shared view produces, so the ordering and grouping
are not decided here -- those are pinned in test_engine_catalog_view.py. What is
pinned here is the translation from a row to what appears on the panel: which
rows exist, which can be focused, and what each one says.

The rows are supplied directly rather than read from disk. That is the point of
the split: the decisions arrive as data, so this screen can be tested without an
engine catalog, a build directory, or a board.
"""

import pytest

from universalchess.i18n import t
from universalchess.menus.engine_manager_menu import (
    CUSTOM_HEADING,
    TIER_HEADING_KEYS,
    build_engine_list_entries,
)
from universalchess.services.engine_catalog_view import EngineRow

# Estimated build time shown for an engine that is not installed yet.
INSTALL_MINUTES = 45

# How far a paused install had got, shown on the row so the pause is visible
# without opening the engine.
PAUSED_PERCENT = 65


class _ResumePoint:
    """A paused install's record; the list only reads its percent."""

    def __init__(self, percent=PAUSED_PERCENT):
        self.percent = percent


def make_row(**overrides) -> EngineRow:
    """An installable, uninstalled, healthy top-tier engine, unless overridden."""
    fields = dict(
        name="reckless",
        display_name="Reckless",
        summary="Rated 3600",
        description="A very strong engine.",
        tier="top",
        elo=3600,
        installed=False,
        needs_repair=False,
        supported=True,
        unsupported_reason=None,
        is_system_package=False,
        can_uninstall=True,
        estimated_install_minutes=INSTALL_MINUTES,
        resume_point=None,
        last_failure=None,
        is_custom=False,
    )
    fields.update(overrides)
    return EngineRow(**fields)


def entry_for(entries, name):
    """The one entry rendering a given engine, failing loudly if absent."""
    matches = [e for e in entries if e.key == name]
    assert len(matches) == 1, f"expected one {name} entry, got {len(matches)}"
    return matches[0]


def selectable_keys(entries):
    """Keys the user can actually focus and press."""
    return [e.key for e in entries if e.selectable and e.enabled]


class TestTheListKeepsTheOrderAndGroupingItIsGiven:
    def test_rows_render_in_the_order_supplied(self):
        """The screen does not re-sort what the shared view decided.

        Why: the board used to sort installed-first then alphabetically, which is
        the rule being retired. If this screen sorts at all, the board and the
        web show the same catalog in different orders again.

        How a regression manifests: the engine keys come back alphabetically, or
        an installed engine jumps above the stronger one above it.
        """
        rows = [
            make_row(name="reckless", display_name="Reckless", elo=3600),
            make_row(name="stockfish", display_name="Stockfish", elo=3500, installed=True),
            make_row(name="berserk", display_name="Berserk", elo=3400),
        ]
        entries = build_engine_list_entries(rows)

        assert [e.key for e in entries if e.selectable] == [
            "reckless",
            "stockfish",
            "berserk",
        ]

    def test_each_group_is_introduced_by_a_heading(self):
        """A tier heading precedes the engines in it, and cannot be focused.

        Why: the board showed one undifferentiated list, so a 1900-rated novelty
        engine looked like a peer of the strongest engine in the catalog. The
        heading is what makes the grouping visible on a screen with no colour or
        badges to carry it.

        How a regression manifests: headings vanish and the list flattens, or a
        heading becomes focusable and pressing it opens a detail screen for an
        engine that does not exist.
        """
        rows = [
            make_row(name="reckless", display_name="Reckless", tier="top"),
            make_row(name="arasan", display_name="Arasan", tier="strong"),
            make_row(name="claudia", display_name="Claudia", tier="specialty"),
        ]
        entries = build_engine_list_entries(rows)

        assert [e.label for e in entries] == [
            t(TIER_HEADING_KEYS["top"]),
            f"Reckless (~{INSTALL_MINUTES}m)\nRated 3600",
            t(TIER_HEADING_KEYS["strong"]),
            f"Arasan (~{INSTALL_MINUTES}m)\nRated 3600",
            t(TIER_HEADING_KEYS["specialty"]),
            f"Claudia (~{INSTALL_MINUTES}m)\nRated 3600",
        ]
        assert not any(e.selectable for e in entries if e.label in {t(key) for key in TIER_HEADING_KEYS.values()})

    def test_a_heading_appears_once_per_group_not_once_per_engine(self):
        """Consecutive engines in the same tier share one heading.

        Why: a heading emitted per row would triple the length of a list that
        already scrolls on a small panel, and would stop reading as a group.

        How a regression manifests: every engine is preceded by its tier name.
        """
        rows = [make_row(name=f"e{i}", tier="top") for i in range(3)]
        entries = build_engine_list_entries(rows)

        assert [e.label for e in entries].count(t(TIER_HEADING_KEYS["top"])) == 1
        assert len(entries) == 4

    def test_an_empty_catalog_renders_no_headings(self):
        """No rows means no group headings for groups with nothing in them.

        Why: headings are emitted as groups change, so a naive implementation
        that pre-seeds one per tier would show three empty sections. This is also
        the zero case for the whole screen.

        How a regression manifests: the list shows bare tier names and nothing
        else.
        """
        assert build_engine_list_entries([]) == []


class TestWhatEachRowSays:
    def test_an_uninstalled_engine_offers_its_build_time(self):
        """The estimate is on the row, before committing to an hour-long build.

        How a regression manifests: the time disappears, and a 45-minute source
        build looks identical to an instant one.
        """
        entry = entry_for(build_engine_list_entries([make_row()]), "reckless")

        assert entry.label == f"Reckless (~{INSTALL_MINUTES}m)\nRated 3600"
        assert entry.icon_name == "checkbox_empty"

    def test_an_installed_engine_is_ticked_and_drops_the_estimate(self):
        """An install time is meaningless for something already installed.

        How a regression manifests: an installed engine advertises a build time,
        implying it must be installed again.
        """
        entry = entry_for(
            build_engine_list_entries([make_row(installed=True)]), "reckless"
        )

        assert entry.label == "Reckless\nRated 3600"
        assert entry.icon_name == "checkbox_checked"

    def test_a_broken_engine_says_so_instead_of_looking_healthy(self):
        """A net-backed engine missing its weights is called out on the row.

        Why: the board showed no repair state, so an engine that could not play
        was indistinguishable from one that could until it was opened and failed.

        How a regression manifests: the row renders as a normal installed engine
        and the user only discovers the problem mid-game.
        """
        entry = entry_for(
            build_engine_list_entries([make_row(installed=True, needs_repair=True)]),
            "reckless",
        )

        assert "Needs repair" in entry.label
        assert entry.selectable is True, "repair is reached through the engine"

    def test_a_paused_install_shows_how_far_it_got(self):
        """A stopped build advertises its resume point on the row.

        Why: preserved build trees are invisible otherwise, and an hour of
        compiling that nobody can see is an hour nobody reclaims.

        How a regression manifests: a paused engine looks uninstalled and offers
        a fresh build, discarding the preserved tree.
        """
        entry = entry_for(
            build_engine_list_entries([make_row(resume_point=_ResumePoint())]),
            "reckless",
        )

        assert f"Paused at {PAUSED_PERCENT}%" in entry.label


class TestThisDeviceRefusesWhatItCannotBuild:
    def test_an_unsupported_engine_is_greyed_out_with_the_reason(self):
        """An engine this CPU cannot build is shown, disabled, and explained.

        Why: the board offered a normal Install row for an engine that cannot
        build here. The install is refused up front, so nothing is destroyed --
        but the user only learned that by pressing it. It stays visible rather
        than hidden, so the catalog reads the same on every device and the reason
        is answerable.

        How a regression manifests: the row is focusable again (pressing it leads
        to an immediate refusal), or it vanishes from the list entirely and the
        engine looks like it does not exist.
        """
        reason = "Requires 64-bit ARM (arm64); this device is armhf"
        rows = [make_row(supported=False, unsupported_reason=reason)]
        entry = entry_for(build_engine_list_entries(rows), "reckless")

        assert entry.enabled is False
        assert reason in entry.label
        assert selectable_keys(build_engine_list_entries(rows)) == []

    def test_a_supported_engine_stays_selectable(self):
        """The gate distinguishes engines rather than disabling all of them.

        Why: an always-disabled list would satisfy the test above while making
        the screen useless.

        How a regression manifests: every engine is greyed out and nothing can be
        installed from the board.
        """
        rows = [make_row(name="ok"), make_row(name="no", supported=False,
                                              unsupported_reason="nope")]
        assert selectable_keys(build_engine_list_entries(rows)) == ["ok"]


class TestCustomEnginesAppearOnTheBoard:
    def test_a_custom_engine_is_listed_under_its_own_heading(self):
        """Operator-added engines reach the device they were added for.

        Why: the board never read the custom registry, so an engine uploaded from
        a phone was invisible on the board. It gets its own heading because it
        carries no rating and belongs in no strength band.

        How a regression manifests: the custom engine is missing from the board,
        or is filed into Specialty among the rated novelty engines.
        """
        rows = [make_row(name="reckless", tier="top"),
                make_row(name="mine", display_name="Mine", tier="specialty",
                         is_custom=True, elo=None, summary="Custom engine")]
        entries = build_engine_list_entries(rows)

        assert [e.label for e in entries][-2] == CUSTOM_HEADING
        assert entry_for(entries, "mine").selectable is True

    def test_the_custom_heading_is_absent_when_there_are_none(self):
        """No custom engines means no empty Custom section.

        How a regression manifests: every board shows a Custom heading with
        nothing beneath it.
        """
        entries = build_engine_list_entries([make_row()])

        assert CUSTOM_HEADING not in [e.label for e in entries]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

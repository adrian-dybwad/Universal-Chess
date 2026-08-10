"""Tests that the board's System menu is ordered the way the web's System tab is.

Background / why these tests exist
----------------------------------
Ordering inside a Settings section is shared for most of them: the web renders
Game, Display and Sound straight from the same catalog containers the board
flattens, so neither can move without the other. System is the exception. Its web
tab is a hand-written sequence of cards, and only one of those cards -- the Device
card, which renders ``group.system.device`` -- comes from the catalog. That left
the rest free to drift, and it did: About was the first thing on the web tab and
the fourth row on the board, between Reset Settings and Power.

The web order wins. The board lists About first, and the rows the two surfaces
share now appear in the same sequence on both. The web's own cards (device clock,
password change, game database, diagnostics) have no board counterpart and sit
between them without affecting that sequence.

There is no catalog container behind the web tab to compare against, so the
expected order is written out here with the card sequence it mirrors. That is the
whole point of the test: it is the only thing tying the two together.
"""

from universalchess.menus.catalog.loader import load_catalog

SYSTEM = "system"
ABOUT = "system.about"

# The board's System rows, in the order the web tab presents the same items:
# system info, then the Device card, then Updates, then Reset Settings, with
# Power last. group.system.device is transparent on the board (the engine inlines
# its children), so it stands for the four Device rows.
EXPECTED_SYSTEM_ORDER = [
    ABOUT,
    "group.system.device",
    "system.updates",
    "system.reset",
    "system.power",
]


def test_about_leads_the_system_menu():
    """About is the first row of the board's System menu.

    Why this test exists: the web opens its System tab with the same information
    (version, hardware, CPU and memory) as its first card, and this was the one
    item the two surfaces disagreed about.

    How a regression manifests: About sinks back down the list, so a user who
    learned one interface hunts for it on the other.
    """
    assert load_catalog().child_ids(SYSTEM)[0] == ABOUT


def test_system_rows_follow_the_web_tab_order():
    """The whole System list matches the web tab's sequence of cards.

    Why this test exists: nothing else compares the two. The board takes this
    order from the catalog and the web tab is hand-written JSX, so only an
    assertion spanning both keeps them together -- which is exactly the gap that
    let About drift in the first place.

    How a regression manifests: a row is added or moved in the catalog and the
    board's System screen no longer reads like the web's System tab, with no
    failure anywhere to say so.
    """
    assert load_catalog().child_ids(SYSTEM) == EXPECTED_SYSTEM_ORDER

"""Tests that an engine's tier is derived from one rule, in one place.

Background / why these tests exist
----------------------------------
The web groups the engine list into Top / Strong / Specialty, and its headings
publish what those mean: "Top Tier Engines (3300+ ELO)", "Strong Engines
(2900-3200 ELO)", "Specialty & Personality Engines". The grouping itself used to
be a pair of hardcoded name arrays in TypeScript, with everything not named in
them falling through to Specialty.

That produced a wrong list rather than merely an untidy one: Reckless, at ~3600
the strongest engine in the catalog, was filed under Specialty purely because
nobody added its name to an array. Adding a sixteenth engine would have done the
same again, silently.

So tier is no longer assigned. Each engine states its rating and the tier follows
from the published bands, computed once in Python and served to the web. An
engine with no meaningful rating -- the human-like and novelty engines -- is
Specialty by definition.
"""

import re

import pytest

from universalchess.managers.engine_manager import (
    ENGINES,
    STRONG_TIER_MIN_ELO,
    TOP_TIER_MIN_ELO,
)

TIERS = ("top", "strong", "specialty")

# The tier each engine must resolve to, pinned so a rating edit that moves an
# engine between bands is a visible, deliberate change rather than a silent
# reshuffle of the list the user sees.
EXPECTED_TIERS = {
    "reckless": "top",
    "stockfish": "top",
    "berserk": "top",
    "koivisto": "top",
    "ethereal": "top",
    "smallbrain": "strong",
    "demolito": "strong",
    "weiss": "strong",
    "arasan": "strong",
    "rodentIV": "specialty",
    "zahak": "specialty",
    "ct800": "specialty",
    "claudia": "specialty",
    "maia": "specialty",
    "worstfish": "specialty",
    "drawfish": "specialty",
}

# Engines that exist for how they play rather than how well, so they carry no
# rating at all.
UNRATED = ("maia", "worstfish", "drawfish")


def test_every_catalog_engine_resolves_to_a_published_tier():
    """Each engine lands in one of the three groups the web renders.

    Why this test exists: the web renders exactly three groups, so a tier value
    outside that set puts an engine in a group that is never drawn -- it simply
    vanishes from the management list with no error anywhere.

    How a regression manifests: a new tier string is introduced on the Python
    side without a matching group on the web, and the engines carrying it
    disappear from the page.
    """
    for name, engine in ENGINES.items():
        assert engine.tier in TIERS, f"{name} has tier {engine.tier!r}"


def test_the_catalog_and_the_pinned_assignment_agree():
    """Every engine is accounted for, in the tier recorded here.

    Why this test exists: the pinned mapping is the record of where each engine
    is meant to appear. Comparing whole dicts (rather than checking the engines
    that happen to be listed) also catches an engine added to the catalog without
    a decision being made about it.

    How a regression manifests: an added or removed engine, or one whose rating
    change moved it between bands, shows up here as a concrete diff instead of
    quietly changing the page.
    """
    assert {name: engine.tier for name, engine in ENGINES.items()} == EXPECTED_TIERS


@pytest.mark.parametrize("name", sorted(n for n in EXPECTED_TIERS if n not in UNRATED))
def test_tier_follows_the_published_elo_bands(name):
    """A rated engine's tier is exactly what its rating and the bands imply.

    Why this test exists: the group headings state the bands to the user, so an
    engine placed against them makes the heading itself false -- a card reading
    "~2700 ELO" under "Strong Engines (2900-3200 ELO)". Deriving the tier is what
    makes that impossible; this asserts the derivation rather than trusting it.

    How a regression manifests: the thresholds and the placement drift apart, and
    the list contradicts its own headings.
    """
    engine = ENGINES[name]
    assert engine.elo is not None, f"{name} is listed as rated but declares no elo"
    if engine.elo >= TOP_TIER_MIN_ELO:
        expected = "top"
    elif engine.elo >= STRONG_TIER_MIN_ELO:
        expected = "strong"
    else:
        expected = "specialty"
    assert engine.tier == expected, f"{name} at {engine.elo} ELO"


@pytest.mark.parametrize("name", UNRATED)
def test_unrated_engines_are_specialty(name):
    """An engine with no rating is Specialty, not accidentally Top.

    Why this test exists: Maia plays like a human and the novelty engines try to
    lose or draw, so a rating would be meaningless for them. The derivation must
    treat a missing rating as "not chosen for strength" rather than defaulting it
    to a number.

    How a regression manifests: a default rating of 0 would still read as
    Specialty, but any non-zero default would promote a novelty engine into a
    strength tier and recommend it to someone looking for a strong opponent.
    """
    engine = ENGINES[name]
    assert engine.elo is None
    assert engine.tier == "specialty"


def test_the_strongest_engine_is_not_filed_under_specialty():
    """Reckless is Top, the specific case the hardcoded arrays got wrong.

    Why this test exists: this is the bug that motivated deriving the tier.
    Reckless outrates every other engine here and was displayed under "Specialty
    & Personality Engines", where nobody looking for the strongest engine would
    find it.

    How a regression manifests: reverting to name-list grouping puts every
    unlisted engine back into Specialty, and this asserts the strongest one is
    not among them.
    """
    reckless = ENGINES["reckless"]
    assert reckless.elo is not None and reckless.elo >= TOP_TIER_MIN_ELO
    assert reckless.tier == "top"
    assert reckless.elo == max(e.elo for e in ENGINES.values() if e.elo is not None)


def test_declared_rating_matches_the_rating_shown_to_the_user():
    """The elo field agrees with the "~N ELO" in the engine's own summary.

    Why this test exists: the rating now exists twice -- as the number the tier
    is derived from, and as prose in the summary the card and the board both
    display. If they drift, a card reads "~2700 ELO" while sitting in a band its
    hidden number qualified it for, which looks like a rendering bug and is not.

    How a regression manifests: a rating is corrected in one place only, so the
    displayed number and the grouping disagree.
    """
    mismatches = []
    for name, engine in ENGINES.items():
        match = re.search(r"~(\d+)\s*ELO", engine.summary)
        if match is None:
            # No advertised rating: only the unrated engines may omit it.
            if engine.elo is not None:
                mismatches.append(f"{name}: elo={engine.elo} but summary quotes none")
            continue
        quoted = int(match.group(1))
        if engine.elo != quoted:
            mismatches.append(f"{name}: elo={engine.elo} but summary says ~{quoted}")
    assert mismatches == [], mismatches

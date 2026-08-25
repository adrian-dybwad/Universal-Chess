"""Tests for the profile label projection.

A profile's label is composed from the profile's own option values, projected
through the probed schema. These tests pin the projection's rules -- which term
each field type renders, which fields are suppressed, and what an empty
projection reports -- because the label reaches the strength picker, the game
card, and the PGN, where a wrong label misstates how strong the opponent is.
"""

import pytest

from universalchess.services.engine_profiles import ProfileField
from universalchess.services import profile_labels as pl


# The schema a UCI_Elo engine (Stockfish-like) probes into: the cap gate, the
# capped Elo declaring its gate and its unit, a bool, and a display-only option.
ELO_FIELDS = (
    ProfileField("UCI_LimitStrength", "Limit strength", "bool", False),
    ProfileField(
        "UCI_Elo", "Elo", "int", 1500, minimum=800, maximum=2800,
        requires="UCI_LimitStrength", unit="ELO",
    ),
    ProfileField("Ponder", "Ponder", "bool", False),
    ProfileField("UCI_EngineAbout", "About", "info", "Stockfish by the devs"),
)

# A file-backed strength selector (Maia-like nets, Rodent-like personalities).
FILE_FIELDS = (
    ProfileField(
        "WeightsFile", "WeightsFile", "select", "/nets/maia-1500.pb.gz",
        options=(("/nets/maia-1500.pb.gz", "maia-1500.pb.gz"),),
        allow_custom=True,
    ),
    ProfileField(
        "PersonalityFile", "PersonalityFile", "select", "/pers/Defender.txt",
        options=(("/pers/Defender.txt", "Defender.txt"),),
        allow_custom=True,
    ),
)

STYLE_FIELD = ProfileField(
    "Style", "Style", "select", "Normal",
    options=(("Solid", "Solid"), ("Normal", "Normal")),
)


# ---------------------------------------------------------------------------
# File terms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/nets/maia-1500.pb.gz", "1500 ELO"),   # embedded rating wins
        ("maia-1100.pb.gz", "1100 ELO"),
        ("/pers/Defender.txt", "Defender"),      # no rating: the stem
        ("Defender", "Defender"),                # already a stem
        ("/books/rodent.bin", "rodent"),
        ("", ""),
    ],
)
def test_a_file_backed_value_renders_as_its_basename_or_embedded_rating(path, expected):
    """A file path must never appear in a label verbatim.

    Why this exists: the stored value is an absolute path, and the label reaches
    the picker, the game card and the PGN. A rating embedded in the filename is
    what the file means (Maia's nets are named for the rating they mimic), so it
    is preferred over the bare stem.

    How a regression manifests: labels read as full paths, or a Maia net reads
    "maia-1500" instead of "1500 ELO" and no longer sorts or scans as a rating.
    """
    assert pl.file_term(path) == expected


# ---------------------------------------------------------------------------
# Single terms, by field type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (ELO_FIELDS[1], "1400", "1400 ELO"),          # int with a declared unit
        (ELO_FIELDS[1], " 1400 ", "1400 ELO"),        # stored text is trimmed
        (ProfileField("Hash", "Hash", "int", 16), "256", "256"),  # unitless int
        (ELO_FIELDS[0], "true", "Limit strength"),    # bool on: renders its label
        (ELO_FIELDS[0], "false", None),               # bool off: omitted
        (STYLE_FIELD, "Solid", "Solid"),              # combo: the chosen value
        (FILE_FIELDS[0], "/nets/maia-1500.pb.gz", "1500 ELO"),
        (ELO_FIELDS[3], "Stockfish", None),           # info is never a term
        (ELO_FIELDS[1], "", None),                    # unset: nothing to say
        (ELO_FIELDS[1], None, None),
    ],
)
def test_each_field_type_renders_its_own_kind_of_term(field, value, expected):
    """The term is derived from the probed field type, not declared per engine.

    Why this exists: rendering by type is what lets a never-seen engine get a
    truthful label with no curation. The unit comes from the field because a bare
    "1400" in the picker does not say what it measures.

    How a regression manifests: an off bool contributes a term (labelling a
    profile with a switch that is not on), a display-only option becomes part of
    the identity users read, or an Elo loses its unit.
    """
    assert pl.term_for(field, value) == expected


# ---------------------------------------------------------------------------
# Composition and suppression
# ---------------------------------------------------------------------------


def test_terms_follow_the_declared_key_order_not_the_schema_order():
    # The projection is an ordered list of keys, so the label reads in the order
    # the engine's own axes were declared. A regression here reorders labels
    # ("1700 ELO: Defender"), which changes every picker row and PGN name.
    fields = FILE_FIELDS + ELO_FIELDS
    values = {
        "PersonalityFile": "/pers/Defender.txt",
        "UCI_LimitStrength": "true",
        "UCI_Elo": "1700",
    }
    assert pl.render_terms(("PersonalityFile", "UCI_Elo"), fields, values) == [
        "Defender",
        "1700 ELO",
    ]
    assert pl.render_terms(("UCI_Elo", "PersonalityFile"), fields, values) == [
        "1700 ELO",
        "Defender",
    ]


def test_a_gated_option_is_suppressed_when_its_gate_is_off():
    """UCI_Elo says nothing about strength while UCI_LimitStrength is false.

    Why this exists: the engine ignores the Elo setting when the cap is off, so
    including it would state a playing strength the engine is not playing at --
    the profile is at full strength. The gate is declared once on the field
    (from the option registry), so this holds for every engine that exposes the
    pair rather than being special-cased per engine.

    How a regression manifests: the uncapped Default profile labels itself
    "1500 ELO" (its leftover Elo value) instead of reporting no terms, and the
    picker offers a maximum-strength profile under a mid-range rating.
    """
    keys = ("UCI_Elo",)
    off = {"UCI_LimitStrength": "false", "UCI_Elo": "1500"}
    on = {"UCI_LimitStrength": "true", "UCI_Elo": "1500"}
    absent = {"UCI_Elo": "1500"}

    assert pl.render_terms(keys, ELO_FIELDS, off) == []
    assert pl.render_terms(keys, ELO_FIELDS, on) == ["1500 ELO"]
    # An engine that exposes no cap gate at all ignores nothing: the Elo stands.
    assert pl.render_terms(keys, ELO_FIELDS, absent) == ["1500 ELO"]


def test_a_key_the_engine_does_not_advertise_contributes_nothing():
    # Configs cross installs (Centaur SD import, backup restore) and engine
    # versions change their option sets, so a stored value can have no field.
    # Rendering it raw would put an unvalidated string of unknown meaning into
    # the label; the regression is a label composed of stale keys.
    values = {"UCI_Elo": "1400", "Gibberish": "42"}
    assert pl.render_terms(("Gibberish", "UCI_Elo"), ELO_FIELDS, values) == ["1400 ELO"]


def test_values_are_matched_case_insensitively():
    # A hand-edited or legacy .uci can spell an option in another case, and the
    # engine matches option names case-insensitively itself. A regression drops
    # the term and silently labels a capped profile as uncapped.
    values = {"uci_limitstrength": "true", "uci_elo": "1200"}
    assert pl.render_terms(("UCI_Elo",), ELO_FIELDS, values) == ["1200 ELO"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"UCI_LimitStrength": "true", "UCI_Elo": "1400"}, "1400 ELO"),
        ({"UCI_LimitStrength": "false", "UCI_Elo": "1400"}, ""),
        ({}, ""),
    ],
)
def test_render_label_joins_the_terms_and_reports_emptiness_as_empty(values, expected):
    """No terms means the label cannot be composed, and the caller must decide.

    Why this exists: an uncapped profile has nothing to say on the strength axis,
    and only the caller knows what to show instead (the picker names it
    "Default (Unlimited)", the game card "Unlimited"). Inventing a label here
    would put a fabricated strength in front of the user.

    How a regression manifests: the empty projection returns a stray separator or
    the word "None", which then appears in the picker and the PGN.
    """
    assert pl.render_label(("UCI_Elo",), ELO_FIELDS, values) == expected


def test_multiple_axes_compose_into_one_label():
    # The point of the projection: a personality capped at an Elo is one profile
    # with two axes, which the old name-as-identity scheme could not express.
    fields = FILE_FIELDS + ELO_FIELDS
    values = {
        "PersonalityFile": "/pers/Defender.txt",
        "UCI_LimitStrength": "true",
        "UCI_Elo": "1700",
    }
    label = pl.render_label(("PersonalityFile", "UCI_Elo"), fields, values)
    assert label == "Defender: 1700 ELO"


# ---------------------------------------------------------------------------
# Key selection
# ---------------------------------------------------------------------------


def test_only_writable_fields_can_be_label_keys():
    # An info option is display-only (validate_profile_values refuses writes to
    # it), so it can never vary between profiles and cannot distinguish them.
    # A regression lets a per-install selection pick it, producing one identical
    # label for every profile.
    assert pl.selectable_keys(ELO_FIELDS) == (
        "UCI_LimitStrength",
        "UCI_Elo",
        "Ponder",
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (("UCI_Elo",), ("UCI_Elo",)),
        (("UCI_Elo", "Nonexistent"), ("UCI_Elo",)),      # unknown keys drop
        (("UCI_EngineAbout", "UCI_Elo"), ("UCI_Elo",)),  # info keys drop
        (("uci_elo",), ("UCI_Elo",)),                    # resolved to the probe's spelling
        (("UCI_Elo", "UCI_Elo"), ("UCI_Elo",)),          # no duplicate terms
        ((), ()),
        (("Nonexistent",), ()),
    ],
)
def test_declared_keys_are_resolved_against_the_probe(declared, expected):
    """A declaration is validated against the engine that is actually installed.

    Why this exists: the key list can come from the catalog or from a per-install
    override in the ``.uci``, and neither is guaranteed to match the binary on
    this device -- an engine update can remove an option. Dropping what the probe
    does not confirm keeps a label from being built out of keys the engine has no
    values for, and resolving the spelling keeps the value lookup exact.

    How a regression manifests: an unknown key renders as an empty term (a label
    beginning with a separator) or raises while building a picker row.
    """
    assert pl.resolve_keys(declared, ELO_FIELDS) == expected

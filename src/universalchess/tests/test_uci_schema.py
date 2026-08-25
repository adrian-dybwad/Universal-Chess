"""Tests for the probe-driven UCI schema service.

Why these tests exist
---------------------
``services.uci_schema`` is what replaces shipped ``.uci`` files: it turns an
engine's live UCI handshake (``chess.engine.Option`` objects) into the editable
:class:`ProfileField` schema the web form renders/validates, and seeds a writable
per-engine config on first use. Because there is no curated data behind it any
more, the correctness of the mapping, the file-picker heuristic, the strength
ladder derivation, and the seeding are the only things standing between a user
and an engine that silently plays wrong (engines ignore unknown options and do
not clamp out-of-range values). These tests pin each of those seams using
fabricated options and temp files, so they need no real engine binary.

The subprocess-touching layer (``probe_options``) is the mocked boundary; every
pure function (mapping, grouping, heuristic, ladder, section derivation) is
exercised directly.
"""

import configparser
import os

import pytest

from universalchess.services import engine_profiles as ep
from universalchess.services import uci_schema as us


class FakeOption:
    """Stand-in for ``chess.engine.Option`` with the attributes the mapper reads.

    python-chess exposes ``name/type/default/min/max/var`` and ``is_managed()``;
    replicating just those keeps the tests independent of a real engine while
    matching the exact surface ``option_to_field`` consumes.
    """

    def __init__(self, name, type, default=None, min=None, max=None, var=None,
                 managed=False):
        self.name = name
        self.type = type
        self.default = default
        self.min = min
        self.max = max
        self.var = var
        self._managed = managed

    def is_managed(self):
        return self._managed


# ---------------------------------------------------------------------------
# option_to_field: UCI type -> ProfileField mapping
# ---------------------------------------------------------------------------


def test_spin_maps_to_int_with_engine_bounds():
    """A spin becomes an int field carrying the engine's own min/max.

    Losing the bounds would let validation accept out-of-range values the engine
    silently mis-applies -- the exact failure the schema exists to prevent.
    """
    field = us.option_to_field(FakeOption("UCI_Elo", "spin", 1500, 800, 2850))
    assert (field.type, field.default, field.minimum, field.maximum) == (
        "int", 1500, 800, 2850,
    )


def test_check_maps_to_bool_default():
    """A check becomes a bool field preserving the advertised default."""
    field = us.option_to_field(FakeOption("UCI_LimitStrength", "check", True))
    assert field.type == "bool"
    assert field.default is True


def test_combo_maps_to_select_over_var_values():
    """A combo becomes a select whose options are its ``var`` entries.

    The var list is the only source of valid values; dropping it would render a
    free text box and let invalid personalities through.
    """
    field = us.option_to_field(
        FakeOption("Personality", "combo", "Default", var=["Default", "Tal", "Petrosian"])
    )
    assert field.type == "select"
    assert field.default == "Default"
    assert field.options == (("Default", "Default"), ("Tal", "Tal"), ("Petrosian", "Petrosian"))
    assert field.allow_custom is False  # fixed enum, no free-text


def test_plain_string_maps_to_text():
    """A string with no file-backing becomes a free-text field."""
    field = us.option_to_field(FakeOption("Comment", "string", "hello"))
    assert field.type == "text"
    assert field.default == "hello"


def test_uci_engine_about_maps_to_info():
    """UCI_EngineAbout is display-only info, not an editable text box.

    Why: the UCI protocol says the GUI should not setoption this string (license
    / about text). How regression shows: type stays 'text' and the form offers
    an input that would write nonsense into the profile.
    """
    about = "Shredder by Stefan Meyer-Kahlen, see www.shredderchess.com"
    field = us.option_to_field(FakeOption("UCI_EngineAbout", "string", about))
    assert field is not None
    assert field.type == "info"
    assert field.default == about


@pytest.mark.parametrize("name", [
    "EngineAbout",
    "engine about",
    "Copyright",
    "UCI_EngineAbout",
])
def test_info_option_names_detected(name):
    """About/copyright option names are classified as informational."""
    assert us.is_info_option(name) is True
    assert us.is_info_option("Comment") is False
    assert us.is_info_option("UCI_Elo") is False


def test_info_option_skips_file_picker_even_if_default_looks_like_path(tmp_path):
    """An About string must never become a file select via the path heuristic."""
    fake_file = tmp_path / "about.txt"
    fake_file.write_text("x", encoding="utf-8")

    def choices(_option):
        return [str(fake_file)]

    field = us.option_to_field(
        FakeOption("UCI_EngineAbout", "string", str(fake_file)), choices
    )
    assert field is not None
    assert field.type == "info"
    assert field.options is None


def test_button_and_managed_options_are_skipped():
    """Buttons (no value) and engine-managed options are not editable.

    MultiPV/Ponder/variant are driven by python-chess; surfacing them would let a
    user fight the engine manager. Buttons have nothing to persist.
    """
    assert us.option_to_field(FakeOption("Clear Hash", "button")) is None
    assert us.option_to_field(FakeOption("MultiPV", "spin", 1, 1, 500, managed=True)) is None


def test_unknown_type_degrades_to_text_rather_than_dropped():
    """A novel/unknown option type stays editable as text, not silently lost.

    Dropping it would make a future engine's option invisible and un-settable;
    degrading to text keeps it configurable.
    """
    field = us.option_to_field(FakeOption("Weird", "quantum", "x"))
    assert field.type == "text"
    assert field.default == "x"


def test_option_to_field_applies_supplied_help():
    """The mapper attaches the supplied help text to the produced field.

    build_groups looks the description up in the registry and passes it here; if
    the mapper ignored it, no probed field would ever carry help and the info
    tooltip/inline hint would always be empty.
    """
    field = us.option_to_field(FakeOption("UCI_Elo", "spin", 1500, 800, 2850),
                               help="Target strength in Elo.")
    assert field.help == "Target strength in Elo."


def test_file_backed_string_becomes_select_with_custom_escape_hatch(tmp_path):
    """A string whose default is a real file becomes a picker of sibling files.

    The enumerated list is a convenience; allow_custom must stay True so a user
    can still point at a net outside the folder. Labels are basenames.
    """
    (tmp_path / "net-1100.pb.gz").write_text("a")
    (tmp_path / "net-1900.pb.gz").write_text("b")
    default = str(tmp_path / "net-1100.pb.gz")

    def choices(option):
        return us.enumerate_file_choices("eng", option, str(tmp_path))

    field = us.option_to_field(FakeOption("WeightsFile", "string", default), choices)
    assert field.type == "select"
    assert field.allow_custom is True
    assert field.options == (
        (str(tmp_path / "net-1100.pb.gz"), "net-1100.pb.gz"),
        (str(tmp_path / "net-1900.pb.gz"), "net-1900.pb.gz"),
    )


# ---------------------------------------------------------------------------
# help_for: option description registry
# ---------------------------------------------------------------------------


def test_help_for_returns_generic_description_case_insensitively():
    """A common option gets its description regardless of advertised name case.

    Engines report option names in varying case (``UCI_Elo`` vs ``uci_elo``); the
    registry is keyed case-insensitively so the description resolves either way. A
    regression that keyed on exact case would silently drop help for the common
    options this feature exists to document.
    """
    assert us.help_for("eng", "UCI_Elo") == us.help_for("eng", "uci_elo")
    assert us.help_for("eng", "UCI_Elo") != ""
    assert us.help_for("eng", "Threads") != ""


def test_help_for_unknown_option_is_empty():
    """An option with no registered description yields empty help (no fabrication).

    Empty is meaningful: the UI simply shows no hint. Returning a plausible-but-
    invented description would mislead the operator about what the knob does.
    """
    assert us.help_for("eng", "SomeVendorSpecificKnob") == ""


def test_help_for_per_engine_overrides_generic():
    """A per-engine description wins over the generic one for the same option.

    Maia's WeightsFile is its strength selector (net == rating), which is more
    specific than the generic 'neural net weights' help; the per-engine entry must
    take precedence. A regression in lookup order would show the generic text.
    """
    generic = us.help_for("someengine", "WeightsFile")
    maia = us.help_for("maia", "WeightsFile")
    assert generic != ""
    assert maia != ""
    assert maia != generic


# ---------------------------------------------------------------------------
# enumerate_file_choices: path heuristic + override registry
# ---------------------------------------------------------------------------


def test_heuristic_matches_only_same_compound_suffix(tmp_path):
    """Sibling enumeration matches the full compound suffix (``.pb.gz``).

    Matching only ``.gz`` would drag in unrelated archives; this pins the
    compound-suffix behaviour by planting a decoy ``.gz`` and a decoy ``.txt``
    that must NOT appear.
    """
    (tmp_path / "a.pb.gz").write_text("a")
    (tmp_path / "b.pb.gz").write_text("b")
    (tmp_path / "decoy.gz").write_text("x")     # different suffix
    (tmp_path / "notes.txt").write_text("y")    # different suffix
    default = str(tmp_path / "a.pb.gz")

    choices = us.enumerate_file_choices("eng", FakeOption("W", "string", default), str(tmp_path))
    assert choices == [str(tmp_path / "a.pb.gz"), str(tmp_path / "b.pb.gz")]


def test_heuristic_returns_none_when_default_is_not_a_file(tmp_path):
    """A string whose default is not an existing path is plain text, not a picker."""
    assert us.enumerate_file_choices(
        "eng", FakeOption("W", "string", "<autodiscover>"), str(tmp_path)
    ) is None


def test_heuristic_returns_none_for_non_string_option(tmp_path):
    """Non file-backed option types never trigger the picker."""
    assert us.enumerate_file_choices(
        "eng", FakeOption("Hash", "spin", 16, 1, 1024), str(tmp_path)
    ) is None


def test_override_registry_enumerates_when_default_is_not_a_path(tmp_path):
    """The override handles engines whose default is not a concrete path.

    Maia/lc0 report ``<autodiscover>``; the override points at the weights
    subdir+glob so the picker still lists installed nets. Regression: without the
    override these engines would show an un-fillable text box.

    The layout mirrors production: Maia installs into a subdirectory
    ``engines/maia/`` whose nets live at ``engines/maia/maia_weights`` (see
    build-maia.sh / the prebuilt directory-copy). ``engines_dir`` here is the
    engines root (``tmp_path``), so the override subdir must include the ``maia``
    component. If the override were ``maia_weights`` (no ``maia/`` prefix), this
    would glob ``engines/maia_weights`` -- which does not exist in production --
    and return None, so the net picker would be empty and no per-rating sections
    would be seeded.
    """
    weights = tmp_path / "maia" / "maia_weights"
    weights.mkdir(parents=True)
    (weights / "maia-1100.pb.gz").write_text("a")
    (weights / "maia-1500.pb.gz").write_text("b")

    choices = us.enumerate_file_choices(
        "maia", FakeOption("WeightsFile", "string", "<autodiscover>"), str(tmp_path)
    )
    assert choices == [
        str(weights / "maia-1100.pb.gz"),
        str(weights / "maia-1500.pb.gz"),
    ]


# ---------------------------------------------------------------------------
# build_groups: grouping + ordering
# ---------------------------------------------------------------------------


def test_build_groups_orders_and_buckets_fields(tmp_path):
    """Strength knobs, engine-wide, then advanced -- each field in its bucket.

    The strength group must come first (primary UX), Hash/Threads go to the
    engine group (they are written to [DEFAULT], not per profile), everything
    else is advanced. A regression that mis-buckets would bury the ELO slider or
    expose engine-wide settings as per-profile.
    """
    options = [
        FakeOption("Contempt", "spin", 0, -100, 100),
        FakeOption("UCI_Elo", "spin", 1500, 800, 2850),
        FakeOption("Threads", "spin", 1, 1, 32),
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("Hash", "spin", 16, 1, 1024),
        FakeOption("Clear Hash", "button"),  # skipped
    ]
    groups = us.build_groups(options, engine_name="eng", engines_dir=str(tmp_path))
    layout = {g.id: [f.key for f in g.fields] for g in groups}

    assert [g.id for g in groups] == ["strength", "engine", "advanced"]
    assert layout["strength"] == ["UCI_Elo", "UCI_LimitStrength"]
    assert layout["engine"] == ["Threads", "Hash"]
    assert layout["advanced"] == ["Contempt"]


def test_build_groups_puts_about_fields_in_about_bucket(tmp_path):
    """Informational strings land in a leading About group.

    Why: about text is not a tuning knob; showing it first (before Strength)
    makes engine identity visible without scrolling past Advanced. How
    regression shows: UCI_EngineAbout appears under advanced with type text,
    is missing from the schema, or is ordered after strength/advanced.
    """
    groups = us.build_groups(
        [
            FakeOption("UCI_Elo", "spin", 1500, 800, 2850),
            FakeOption(
                "UCI_EngineAbout",
                "string",
                "Engine X, see https://example.com/engine",
            ),
        ],
        engine_name="eng",
        engines_dir=str(tmp_path),
    )
    assert [g.id for g in groups] == ["about", "strength"]
    about = groups[0].fields[0]
    assert about.key == "UCI_EngineAbout"
    assert about.type == "info"


def test_build_groups_populates_help_from_registry(tmp_path):
    """Probed fields carry the registry's description end-to-end.

    Probing yields no help text, so build_groups must inject it from the registry;
    this asserts a known option (UCI_Elo) ends up with non-empty help that also
    survives JSON serialization (what the web form actually reads). A regression
    that stopped wiring help would leave every probed field's help empty.
    """
    groups = us.build_groups(
        [FakeOption("UCI_Elo", "spin", 1500, 800, 2850)],
        engine_name="eng", engines_dir=str(tmp_path),
    )
    field = groups[0].fields[0]
    assert field.help == us.help_for("eng", "UCI_Elo")
    assert field.help != ""

    payload = ep.schema_to_json(groups)
    assert payload[0]["fields"][0]["help"] == field.help


def test_build_groups_omits_empty_groups(tmp_path):
    """Groups with no fields are dropped so the form has no empty sections."""
    groups = us.build_groups(
        [FakeOption("Contempt", "spin", 0, -100, 100)],
        engine_name="eng", engines_dir=str(tmp_path),
    )
    assert [g.id for g in groups] == ["advanced"]


def test_file_backed_selector_is_grouped_as_strength(tmp_path):
    """A net-file picker is the strength selector for its engine.

    Maia has no UCI_Elo; its net file IS the strength control, so it belongs in
    the strength group, not buried under advanced.
    """
    (tmp_path / "net-1500.pb.gz").write_text("a")
    default = str(tmp_path / "net-1500.pb.gz")
    groups = us.build_groups(
        [FakeOption("WeightsFile", "string", default)],
        engine_name="eng", engines_dir=str(tmp_path),
    )
    assert [g.id for g in groups] == ["strength"]
    assert groups[0].fields[0].key == "WeightsFile"


# ---------------------------------------------------------------------------
# _elo_ladder + _file_level_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("minimum,maximum,expected", [
    (800, 2850, [800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800]),
    (1320, 2850, [1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800]),  # start rounded up
    (2600, 2700, [2600]),      # sub-step range yields the low end
    (2000, 2000, [2000]),      # single point
])
def test_elo_ladder_steps_within_range(minimum, maximum, expected):
    """The ladder stays inside [min,max] and steps by the fixed granularity.

    A regression stepping outside the range would seed profiles the engine
    rejects (UCI_Elo out of bounds); rounding the start keeps steps 'round'.
    """
    assert us._elo_ladder(minimum, maximum) == expected


@pytest.mark.parametrize("minimum,maximum", [(None, 2850), (800, None), (2850, 800)])
def test_elo_ladder_empty_when_range_unusable(minimum, maximum):
    """No ladder when a bound is missing or inverted (no strength profiles seeded)."""
    assert us._elo_ladder(minimum, maximum) == []


@pytest.mark.parametrize("path,expected", [
    ("/x/maia-1100.pb.gz", "1100 ELO"),   # embedded elo preferred
    ("/x/weights_1900.pb.gz", "1900 ELO"),
    ("/x/strong.pb.gz", "strong"),        # no number -> stem
])
def test_a_file_backed_section_is_named_by_the_label_renderer(path, expected):
    """The file naming rule has one implementation, the label projection's.

    Why this exists: the seeder used to name file sections with its own copy of
    this rule (``_file_level_name``). With labels projected from values, a second
    copy could disagree -- a section seeded ``1100 ELO`` but labelled
    ``maia-1100`` reads as two different profiles between the picker and the game
    card. The names the seeder produces through this renderer are asserted by
    ``test_derive_sections_file_levels_take_precedence``.
    """
    assert us.profile_labels.file_term(path) == expected


# ---------------------------------------------------------------------------
# Option registry: dependencies and units
# ---------------------------------------------------------------------------


def test_the_elo_field_carries_the_gate_the_engine_never_advertises():
    """UCI_Elo's dependency on UCI_LimitStrength reaches consumers via the field.

    Why this exists: the UCI handshake describes each option alone and never says
    that the engine ignores UCI_Elo while the cap is off. That rule is declared
    once in this module's registry and carried on the field, so the label
    projection (and anything else asking what the engine is actually applying)
    reads one declaration instead of hardcoding the pair.

    How a regression manifests: an uncapped profile keeps a leftover UCI_Elo
    value, and with no gate on the field it is labelled and reported as playing
    at that rating while the engine plays at full strength.
    """
    elo = us.option_to_field(FakeOption("UCI_Elo", "spin", 1500, 800, 2850))
    assert (elo.requires, elo.unit) == ("UCI_LimitStrength", "ELO")


@pytest.mark.parametrize("option", [
    FakeOption("Hash", "spin", 16, 1, 1024),
    FakeOption("UCI_LimitStrength", "check", False),
    FakeOption("Personality", "combo", "Default", var=["Default", "Tal"]),
    FakeOption("Comment", "string", "hello"),
])
def test_an_option_with_no_declared_dependency_or_unit_carries_neither(option):
    # Nothing may be invented: a fabricated unit would label Hash's 256 as a
    # rating, and a fabricated gate would silently drop a term from every label.
    field = us.option_to_field(option)
    assert (field.requires, field.unit) == ("", "")


# ---------------------------------------------------------------------------
# Label keys
# ---------------------------------------------------------------------------


def test_label_keys_are_the_axes_the_ladder_is_seeded_from(tmp_path):
    """The label projects the same axes seeding varies, for the same engine.

    Why this exists: if the two disagree, seeded sections get labels that do not
    distinguish them -- an engine laddered by net would label every rung by its
    Elo option (unset, so blank), or an Elo-laddered engine would label by a book
    filename. Deriving the keys from the same schema is what keeps them together;
    this pins it for both engine shapes.

    How a regression manifests: the picker shows several identically labelled
    rows, which is indistinguishable to a user from the ladder having collapsed.
    """
    for elo in ("1100", "1500"):
        (tmp_path / f"net-{elo}.pb.gz").write_text("x")
    file_options = [FakeOption("WeightsFile", "string", str(tmp_path / "net-1100.pb.gz"))]
    elo_options = [
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
        FakeOption("Hash", "spin", 16, 1, 1024),
    ]

    file_groups = us.build_groups(
        file_options, engine_name="eng", engines_dir=str(tmp_path)
    )
    elo_groups = us.build_groups(elo_options, engine_name="eng")

    assert us.default_label_keys(file_groups) == ("WeightsFile",)
    assert us.default_label_keys(elo_groups) == ("UCI_Elo",)

    # The keys name exactly the options the seeded sections set, gates aside.
    for options, groups, engines_dir in (
        (file_options, file_groups, str(tmp_path)),
        (elo_options, elo_groups, str(tmp_path / "no-files")),
    ):
        keys = set(us.default_label_keys(groups))
        seeded = us.derive_sections(
            options, engine_name="eng", engines_dir=engines_dir
        )
        varied = {
            key
            for name, values in seeded
            for key in values
            if name != "Default" and key != "UCI_LimitStrength"
        }
        assert varied == keys


def test_a_gate_option_is_never_a_label_key():
    # UCI_LimitStrength is in the strength group and is writable, but on its own
    # it says nothing: the option it gates reports its own suppression. Including
    # it would label every capped profile "Limit strength: 1400 ELO".
    groups = us.build_groups(
        [
            FakeOption("UCI_LimitStrength", "check", False),
            FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
        ],
        engine_name="eng",
    )
    assert us.default_label_keys(groups) == ("UCI_Elo",)


def test_a_catalog_declaration_chooses_between_ambiguous_file_axes(tmp_path, monkeypatch):
    """A declared axis wins over the probe order, which cannot know the axis.

    Why this exists: an engine can advertise several file-backed options, and the
    derived default takes them in handshake order. Labelling a profile by its
    opening book instead of its playing style is wrong in a way no user can
    correct, so the catalog can name the axis.

    How a regression manifests: the declaration is ignored (labels follow probe
    order again) or trusted blindly, so an engine update that removed the option
    leaves profiles labelled by a key with no value.
    """
    (tmp_path / "Defender.txt").write_text("x")
    (tmp_path / "Attacker.txt").write_text("x")
    (tmp_path / "book.bin").write_text("x")
    (tmp_path / "gm.bin").write_text("x")
    options = [
        FakeOption("BookFile", "string", str(tmp_path / "book.bin")),
        FakeOption("PersonalityFile", "string", str(tmp_path / "Defender.txt")),
    ]
    groups = us.build_groups(options, engine_name="rodentIV", engines_dir=str(tmp_path))

    # Derived: handshake order, which puts the book first.
    assert us.default_label_keys(groups)[0] == "BookFile"

    monkeypatch.setattr(
        us, "catalog_label_keys", lambda name: ("PersonalityFile",)
    )
    assert us.profile_label_keys("rodentIV", groups) == ("PersonalityFile",)

    # A declaration the installed engine cannot satisfy falls back to the probe
    # rather than leaving the profile with no label at all.
    monkeypatch.setattr(us, "catalog_label_keys", lambda name: ("GoneInV5",))
    assert us.profile_label_keys("rodentIV", groups) == us.default_label_keys(groups)


def _rodent_options():
    """The options Rodent IV advertises once its personalities are installed.

    Taken from a handshake against a built binary with ``personalities/`` beside
    it, which is how the installer lays it out. Rodent reads
    ``personalities/basic.ini`` relative to its own executable, and that file
    turns the aliases it defines into a ``Personality`` combo (hiding the
    ``PersonalityFile`` string it offers otherwise) while suppressing the book
    options entirely, because it takes books from the personality.
    """
    return [
        FakeOption("Hash", "spin", 16, 1, 4096),
        FakeOption("UCI_LimitStrength", "check", True),
        FakeOption("UCI_Elo", "spin", 2800, 800, 2800),
        FakeOption(
            "Personality", "combo", "---", var=["---", "Defender", "Fischer", "Tal"]
        ),
        FakeOption("Contempt", "spin", 0, -500, 500),
    ]


def test_rodents_playing_style_is_part_of_its_profile_labels():
    """Rodent's strength axis is a combo, so the catalog has to declare it.

    Why this exists: the derived keys are the axes seeding varies -- file-backed
    selectors and ``UCI_Elo`` -- and Rodent's 30-odd playing styles are neither.
    They arrive as a ``Personality`` combo, so without the catalog declaration
    two profiles differing only in style would carry the same label, and the
    picker would offer several rows a user cannot tell apart.

    How a regression manifests: the declaration is dropped or misspelled, and the
    style disappears from the label -- leaving "1700 ELO" for a profile that also
    chose Defender, or an empty label for an uncapped one that only chose a style.
    """
    groups = us.build_groups(_rodent_options(), engine_name="rodentIV")

    assert us.profile_label_keys("rodentIV", groups) == ("Personality", "UCI_Elo")

    # The style belongs beside the Elo in the form, not among the tuning knobs:
    # for this engine it is how strength is chosen.
    strength = {
        field.key for group in groups if group.id == "strength" for field in group.fields
    }
    assert strength == {"UCI_LimitStrength", "UCI_Elo", "Personality"}

    projection = us.profile_labels.LabelProjection(
        keys=us.profile_label_keys("rodentIV", groups),
        fields=tuple(field for group in groups for field in group.fields),
    )
    assert projection.label(
        {"Personality": "Defender", "UCI_LimitStrength": "true", "UCI_Elo": "1700"}
    ) == "Defender: 1700 ELO"
    # Uncapped: the Elo term is suppressed by its own gate, and the style is all
    # that is left to say about the profile.
    assert projection.label(
        {"Personality": "Defender", "UCI_LimitStrength": "false", "UCI_Elo": "1700"}
    ) == "Defender"


def test_rodent_without_its_personalities_is_labelled_by_elo_alone():
    """A declaration the binary cannot satisfy degrades to the derived axes.

    Why this exists: Rodent only advertises ``Personality`` when it finds its
    ``personalities`` directory. If the declaration were trusted rather than
    resolved against the probe, an install missing those files would project
    every profile through a key that has no field, producing an empty label for
    every row of the picker.

    How a regression manifests: the labels come back empty (no term renders) and
    the strength picker shows blank rows.
    """
    options = [o for o in _rodent_options() if o.name != "Personality"]
    groups = us.build_groups(options, engine_name="rodentIV")

    assert us.profile_label_keys("rodentIV", groups) == ("UCI_Elo",)


def _elo_groups():
    """Schema of an engine with a capped Elo, a gate, and an engine-wide option."""
    return us.build_groups(
        [
            FakeOption("UCI_LimitStrength", "check", False),
            FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
            FakeOption("Hash", "spin", 16, 1, 1024),
            FakeOption("Contempt", "spin", 0, -100, 100),
        ],
        engine_name="eng",
    )


def _write_uci(tmp_path, selection=None, body="[Default]\n"):
    """Write a .uci carrying an optional ``[DEFAULT] ProfileLabel`` selection."""
    path = tmp_path / "eng.uci"
    default = "[DEFAULT]\nHash = 16\n"
    if selection is not None:
        default += f"ProfileLabel = {selection}\n"
    path.write_text(f"{default}\n{body}", encoding="utf-8")
    return str(path)


def test_this_installs_selection_outranks_the_catalog_and_the_probe(tmp_path):
    """The per-install selection is the most specific answer, so it wins.

    Why this exists: which options identify a profile depends on the install --
    the engine build, the files present -- and the operator is the only one who
    can know. Without this the selection is stored and ignored, which is worse
    than not offering it.

    How a regression manifests: labels keep following the catalog/derived keys
    after the file is edited, with nothing to indicate the selection was read.
    """
    groups = _elo_groups()
    path = _write_uci(tmp_path, selection="Contempt")
    projection = us.label_projection("eng", groups, path)

    assert projection.keys == ("Contempt",)
    assert projection.label({"Contempt": "25"}) == "25"


@pytest.mark.parametrize("selection", ["Gibberish", "UCI_EngineAbout", ""])
def test_a_selection_the_probe_cannot_confirm_is_dropped(tmp_path, selection):
    # Unknown keys (an engine update removed the option, or the file was
    # hand-edited with a typo) and display-only options cannot label anything.
    # A regression renders them as empty terms, so every label reads as a stray
    # separator or is blank.
    groups = _elo_groups()
    projection = us.label_projection("eng", groups, _write_uci(tmp_path, selection))
    assert projection.keys == us.profile_label_keys("eng", groups)


def test_a_selection_that_describes_no_profile_falls_back_rather_than_blank(tmp_path):
    """Hash is a real option, but it never appears in a profile section.

    Why this exists: the guard the plan calls for. ``Hash`` and ``Threads`` are
    engine-wide and live in the shared ``[DEFAULT]`` section, so a profile's own
    values never contain them: a selection of ``Hash`` alone resolves against the
    probe, renders nothing for every profile, and would leave the strength picker
    a list of identical blank rows with nothing to choose between.

    How a regression manifests: exactly that -- blank picker rows and a blank
    strength in the game card and the PGN.
    """
    groups = _elo_groups()
    projection = us.label_projection("eng", groups, _write_uci(tmp_path, "Hash"))
    capped = {"UCI_LimitStrength": "true", "UCI_Elo": "1600"}

    assert projection.keys == ("Hash",)
    assert projection.fallback == ("UCI_Elo",)
    assert projection.label(capped) == "1600 ELO"
    # Nothing to fall back to for an uncapped profile: it is genuinely unlabelled
    # on the strength axis, and the caller names it (Unlimited).
    assert projection.label({"UCI_LimitStrength": "false", "UCI_Elo": "1600"}) == ""


def test_an_install_with_no_selection_uses_the_catalog_or_derived_keys(tmp_path):
    # The common case, and the one that must not depend on the file existing at
    # all: a fresh install has no .uci until the engine is first probed.
    groups = _elo_groups()
    derived = us.profile_label_keys("eng", groups)
    assert us.label_projection("eng", groups, _write_uci(tmp_path)).keys == derived
    assert us.label_projection(
        "eng", groups, str(tmp_path / "absent.uci")
    ).keys == derived


def test_no_catalog_engine_declares_a_label_key_that_is_not_a_string():
    # The declaration is read from the catalog and handed to the projection; a
    # non-string key would be dropped silently, so this catches a malformed entry
    # at its source instead of as a missing label term at runtime.
    from universalchess.managers.engine_manager import ENGINES

    for name, engine in ENGINES.items():
        label = engine.profile_label
        if label is None:
            continue
        assert label.keys, f"{name} declares an empty ProfileLabel"
        assert all(isinstance(key, str) and key for key in label.keys), name


# ---------------------------------------------------------------------------
# derive_sections: what gets seeded
# ---------------------------------------------------------------------------


def test_derive_sections_builds_elo_ladder_with_limit_flag():
    """UCI_Elo + UCI_LimitStrength -> Default(off) plus a ladder that turns it on.

    Each ELO section must set UCI_LimitStrength=true or the engine ignores
    UCI_Elo and plays full strength; Default must leave the limit off.
    """
    options = [
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
    ]
    sections = us.derive_sections(options, engine_name="eng")

    assert sections[0] == ("Default", {"UCI_LimitStrength": "false"})
    assert [values for _, values in sections[1:]] == [
        {"UCI_LimitStrength": "true", "UCI_Elo": "1400"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1600"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1800"},
    ]


def test_every_seeded_section_is_identified_by_a_generated_id():
    """Sections are named by id; what they mean lives in their values.

    Why this exists: the section name used to be the identity, the stored foreign
    key, the URL segment and the display label at once, so a profile could not be
    relabelled without moving it, and a rung named for an Elo it no longer sets
    stated a strength it did not play. Ids are opaque and the label is projected
    from the values, so there is one copy of the Elo.

    Ids must also be unique and unpredictable: ``reset_config`` re-derives the
    ladder from a fresh probe, and an ordinal id would resolve a stored reference
    to whatever rung now sits in that position -- a different strength, silently.

    How a regression manifests: two rungs share an id (one becomes unreachable,
    and ConfigParser keeps only the last), or ids come out sequential and a
    reference into a re-derived ladder resolves to the wrong strength.
    """
    options = [
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 800, 2800),
    ]
    ids = [name for name, _ in us.derive_sections(options, engine_name="eng")]
    again = [name for name, _ in us.derive_sections(options, engine_name="eng")]

    assert ids[0] == "Default"
    assert len(set(ids)) == len(ids)
    assert all(ep.is_profile_id(name) for name in ids[1:])
    assert all(ep.is_valid_profile_name(name) for name in ids[1:])
    # A second derivation of the same ladder issues different ids.
    assert set(ids[1:]).isdisjoint(again[1:])


def test_a_rated_file_axis_seeds_one_rung_per_file_with_default_in_the_middle(tmp_path):
    """For Maia the nets ARE the ladder, so Default copies the middle net.

    A fresh install should be neither the weakest nor the strongest net, and the
    files are named for the rating they mimic, so the middle one is a meaningful
    default. How a regression manifests: Default selects no net at all and lc0
    refuses to search, or it selects the weakest.
    """
    for elo in ("1100", "1500", "1900"):
        (tmp_path / f"net-{elo}.pb.gz").write_text("x")
    default = str(tmp_path / "net-1100.pb.gz")
    options = [FakeOption("WeightsFile", "string", default)]

    sections = us.derive_sections(options, engine_name="eng", engines_dir=str(tmp_path))

    assert sections[0] == ("Default", {"WeightsFile": str(tmp_path / "net-1500.pb.gz")})
    assert [values for _, values in sections[1:]] == [
        {"WeightsFile": str(tmp_path / f"net-{elo}.pb.gz")}
        for elo in ("1100", "1500", "1900")
    ]


def test_an_unrated_file_axis_leaves_default_uncapped(tmp_path):
    """Default means "as the engine comes" for every engine, including this one.

    Why this exists: Default used to be the alphabetically middle file whatever
    the files were. For Maia's rating-named nets that is a middle strength; for a
    set of playing-style files it is an arbitrary style presented as the engine's
    default, and there is no way for a user to get the engine's own behaviour
    back.

    How a regression manifests: selecting Default silently loads a personality --
    the board plays in a style nobody chose, and it cannot be turned off.
    """
    for name in ("Attacker", "Defender", "Positional"):
        (tmp_path / f"{name}.txt").write_text("x")
    options = [
        FakeOption("PersonalityFile", "string", str(tmp_path / "Attacker.txt")),
    ]

    sections = us.derive_sections(options, engine_name="eng", engines_dir=str(tmp_path))

    assert sections[0] == ("Default", {})
    assert [values for _, values in sections[1:]] == [
        {"PersonalityFile": str(tmp_path / f"{name}.txt")}
        for name in ("Attacker", "Defender", "Positional")
    ]


def test_both_axes_are_seeded_when_the_engine_exposes_both(tmp_path):
    """A file axis no longer suppresses the Elo ladder.

    Why this exists: the file branch returned before the Elo ladder was built, so
    an engine with both a personality selector and UCI_Elo got personalities and
    no ratings -- half its strength control was unreachable, and a profile that
    is a personality capped at a rating could not exist.

    Axes are seeded side by side rather than crossed: the product of the two
    would be a picker of hundreds of rows. Each rung differs from Default on one
    axis, and a combined profile is composed by editing one.

    How a regression manifests: the returned ladder holds only one axis, or it
    holds the cross product and the picker becomes unusable.
    """
    for name in ("Attacker", "Defender"):
        (tmp_path / f"{name}.txt").write_text("x")
    options = [
        FakeOption("PersonalityFile", "string", str(tmp_path / "Attacker.txt")),
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1600),
    ]

    sections = us.derive_sections(options, engine_name="eng", engines_dir=str(tmp_path))
    values = [v for _, v in sections]

    assert values == [
        {"UCI_LimitStrength": "false"},
        {"UCI_LimitStrength": "false", "PersonalityFile": str(tmp_path / "Attacker.txt")},
        {"UCI_LimitStrength": "false", "PersonalityFile": str(tmp_path / "Defender.txt")},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1400"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1600"},
    ]


def test_the_declared_axis_decides_which_file_option_seeds_the_ladder(tmp_path, monkeypatch):
    """An opening book must not become an engine's strength ladder.

    Why this exists: the seeder took the first file-backed option in handshake
    order, and an engine can advertise a style selector and a book both. A ladder
    of opening books is not a strength ladder, and which one comes first is not
    something the probe can be asked. The catalog declaration that names the label
    axis names this one too, so the two cannot disagree.

    How a regression manifests: the ladder is seeded from the wrong axis, and
    every profile in the picker is named after an opening book.
    """
    (tmp_path / "book.bin").write_text("x")
    (tmp_path / "gm.bin").write_text("x")
    (tmp_path / "Defender.txt").write_text("x")
    (tmp_path / "Attacker.txt").write_text("x")
    options = [
        FakeOption("BookFile", "string", str(tmp_path / "book.bin")),
        FakeOption("PersonalityFile", "string", str(tmp_path / "Defender.txt")),
    ]

    monkeypatch.setattr(us, "catalog_label_keys", lambda name: ("PersonalityFile",))
    sections = us.derive_sections(
        options, engine_name="rodentIV", engines_dir=str(tmp_path)
    )

    assert [set(values) for _, values in sections[1:]] == [
        {"PersonalityFile"}, {"PersonalityFile"},
    ]


def test_derive_sections_without_strength_controls_is_default_only():
    """An engine exposing no strength knob seeds just an empty Default section."""
    options = [FakeOption("Hash", "spin", 16, 1, 1024)]
    assert us.derive_sections(options, engine_name="eng") == [("Default", {})]


# ---------------------------------------------------------------------------
# get_schema: probe boundary mocked
# ---------------------------------------------------------------------------


def test_get_schema_uses_probe_and_returns_groups(monkeypatch):
    """get_schema wires the probe output through build_groups.

    Mocks the subprocess boundary (probe_options) only; the mapping/grouping is
    the real code under test.
    """
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("UCI_Elo", "spin", 1500, 800, 2850),
    ])
    groups = us.get_schema("eng", engine_path="/fake/eng")
    assert [g.id for g in groups] == ["strength"]
    assert groups[0].fields[0].key == "UCI_Elo"


def test_get_schema_raises_when_binary_missing(monkeypatch):
    """A missing binary surfaces as EngineProbeError, not a generic crash.

    Callers (endpoints) turn this into an 'editable: false' response; a different
    exception would leak as a 500.
    """
    monkeypatch.setattr(us, "get_engine_path", lambda name: None)
    with pytest.raises(us.EngineProbeError):
        us.get_schema("eng")


# ---------------------------------------------------------------------------
# probe_options: the registry boundary
#
# Why this section exists
# -----------------------
# probe_options is the only place the schema service touches a real process.
# Releasing a *shared* handle deliberately does not quit it -- reaping is
# deferred to evict_unused() at a game-teardown boundary. The web process has no
# such boundary and never calls evict_unused(), so before this the probe left one
# idle engine process resident per distinct engine it had ever probed, for the
# lifetime of the service. The probe must therefore reap by name itself.
# ---------------------------------------------------------------------------


class FakeHandle:
    """Stand-in for EngineHandle exposing just the options dict the probe reads."""

    def __init__(self, path, options):
        self.path = path
        self.engine = type("FakeEngine", (), {"options": options})()


class RecordingRegistry:
    """Registry double recording the acquire/release/evict calls the probe makes.

    Raises ``load_error`` from ``acquire_or_raise`` when one is supplied, so the
    failure path can be exercised without a real binary.
    """

    def __init__(self, options=None, load_error=None):
        self._options = {} if options is None else options
        self._load_error = load_error
        self.calls = []

    def acquire_or_raise(self, engine_path):
        self.calls.append(("acquire", engine_path))
        if self._load_error is not None:
            raise self._load_error
        return FakeHandle(engine_path, self._options)

    def release(self, handle):
        self.calls.append(("release", handle.path))

    def evict_if_unused(self, engine_path):
        self.calls.append(("evict", engine_path))
        return True


def test_probe_options_releases_and_reaps_the_engine_it_used(monkeypatch):
    """A successful probe leaves no engine process behind.

    Why this test exists: the web process never calls evict_unused(), so any
    engine the probe pools stays resident until the service restarts. Opening the
    profile editor for four engines left four idle engine processes in
    universal-chess-web. The probe must release and then reap by name.

    How the regression manifests: dropping the evict call leaves the call log
    without an ("evict", ...) entry -- on a board that is one leaked process per
    engine probed, invisible until memory runs out.
    """
    registry = RecordingRegistry(options={"UCI_Elo": FakeOption("UCI_Elo", "spin", 1500)})
    monkeypatch.setattr(us, "get_engine_registry", lambda: registry)

    options = us.probe_options("/fake/bin/ct800")

    assert [o.name for o in options] == ["UCI_Elo"]
    assert registry.calls == [
        ("acquire", "/fake/bin/ct800"),
        ("release", "/fake/bin/ct800"),
        ("evict", "/fake/bin/ct800"),
    ], "the probe must release before reaping, and must reap by name"


def test_probe_options_reaps_when_the_launch_fails(monkeypatch):
    """A failed probe reaps too, and never releases a handle it does not hold.

    Why this test exists: the failure path is the one that repeats -- a user
    retrying a broken engine probes it over and over. Cleanup lives in a finally
    block, so it must tolerate there being no handle at all.

    How the regression manifests: calling release(None) raises AttributeError
    from the finally block, replacing the informative EngineProbeError with a
    500; skipping the evict re-introduces the leak on exactly the hot path.
    """
    from universalchess.services.engine_registry import EngineLoadError

    registry = RecordingRegistry(
        load_error=EngineLoadError("could not launch", reason_code="incompatible_binary")
    )
    monkeypatch.setattr(us, "get_engine_registry", lambda: registry)

    with pytest.raises(us.EngineProbeError):
        us.probe_options("/fake/bin/ct800")

    assert registry.calls == [
        ("acquire", "/fake/bin/ct800"),
        ("evict", "/fake/bin/ct800"),
    ], "no release without a handle; still reap"


def test_probe_options_propagates_the_reason_code(monkeypatch):
    """EngineProbeError carries the classified reason from the registry.

    Why this test exists: this code is the whole reason the failure becomes
    visible in the UI instead of only in the journal. If probe_options discarded
    it, every endpoint above would be back to reporting a bare "not installed".

    How the regression manifests: reason_code is None (or the attribute is
    missing) and the API falls back to its generic message.
    """
    from universalchess.services.engine_registry import EngineLoadError

    registry = RecordingRegistry(
        load_error=EngineLoadError("could not launch", reason_code="crashed_at_startup")
    )
    monkeypatch.setattr(us, "get_engine_registry", lambda: registry)

    with pytest.raises(us.EngineProbeError) as excinfo:
        us.probe_options("/fake/bin/ct800")

    assert excinfo.value.reason_code == "crashed_at_startup"


def test_probe_error_for_a_missing_binary_reports_binary_missing(monkeypatch):
    """get_schema distinguishes "no binary" from "binary will not start".

    Why this test exists: these two produce identical user-facing text today,
    which is what makes the card ("Installed") and the editor ("not installed")
    contradict each other. The reason code is what lets the endpoint tell them
    apart without probing anything extra.

    How the regression manifests: reason_code is None for the missing-binary
    case, so the endpoint cannot pick the truthful message.
    """
    monkeypatch.setattr(us, "get_engine_path", lambda name: None)

    with pytest.raises(us.EngineProbeError) as excinfo:
        us.get_schema("eng")

    assert excinfo.value.reason_code == "binary_missing"


# ---------------------------------------------------------------------------
# has_seeded_profiles: readiness from disk, without launching anything
#
# The engines list renders every catalog engine at once, so it cannot probe --
# that would be one process per card. The seeded .uci already on disk is proof a
# probe once succeeded, and a stat plus a small ini parse costs nothing.
# ---------------------------------------------------------------------------


def test_has_seeded_profiles_false_when_config_absent(tmp_path):
    """No .uci means the post-install probe never produced one.

    Why this test exists: this is the CT800 case -- binary installed, probe
    failed, no config written, so the board's Elo menu falls back to a lone
    "Default". The card must be able to see that without launching the engine.

    How the regression manifests: returning True for a missing file makes the
    card claim profiles are ready and hides the failure again.
    """
    assert us.has_seeded_profiles("eng", config_path=str(tmp_path / "eng.uci")) is False


def test_has_seeded_profiles_false_for_default_only_config(tmp_path):
    """A config carrying only [DEFAULT] is not a usable strength ladder.

    Why this test exists: seed_config always writes [DEFAULT] with Threads, so
    file existence alone is not proof of a successful derivation -- a
    Default-only file is the known stuck state that "Reset profiles" exists to
    heal. Treating it as ready would leave the user with no rungs and no warning.

    How the regression manifests: an existence-only check returns True here, and
    the engine card looks healthy while the Elo picker is empty.
    """
    config = tmp_path / "eng.uci"
    config.write_text("[DEFAULT]\nThreads = 1\n\n")

    assert us.has_seeded_profiles("eng", config_path=str(config)) is False


def test_has_seeded_profiles_true_when_ladder_present(tmp_path):
    """A config with derived sections reports ready.

    Why this test exists: the positive case must not be starved by an
    over-strict check, or every healthy engine gets a warning badge.

    How the regression manifests: returning False here flags every working
    engine as broken -- far more visible than the bug being fixed, and the
    reason this assertion is paired with the two negatives above.
    """
    config = tmp_path / "eng.uci"
    config.write_text(
        "[DEFAULT]\nThreads = 1\n\n"
        "[Default]\nUCI_LimitStrength = false\n\n"
        "[1600 ELO]\nUCI_LimitStrength = true\nUCI_Elo = 1600\n\n"
    )

    assert us.has_seeded_profiles("eng", config_path=str(config)) is True


def test_has_seeded_profiles_false_for_an_unreadable_config(tmp_path):
    """A corrupt .uci reports not-ready instead of raising.

    Why this test exists: this runs once per card on every load of the engines
    list. A parse error there would take down the whole page rather than flagging
    one engine, and a corrupt file genuinely is not a usable ladder.

    How the regression manifests: configparser.MissingSectionHeaderError escapes
    and GET /api/engines/all returns 500, so no engine renders at all.
    """
    config = tmp_path / "eng.uci"
    config.write_text("this is not an ini file\n")

    assert us.has_seeded_profiles("eng", config_path=str(config)) is False


# ---------------------------------------------------------------------------
# seed_config: lazy, atomic, idempotent
# ---------------------------------------------------------------------------


def _profile_values(config_path):
    """Return the seeded sections' values in file order, [DEFAULT] excluded."""
    return [profile["values"] for profile in ep.read_profiles(str(config_path))]


def test_seed_config_writes_default_section_and_sections(monkeypatch, tmp_path):
    """Seeding writes [DEFAULT] Threads plus the derived strength sections.

    The seeded file must be exactly what the engine player and pickers read;
    this asserts the DEFAULT engine-wide block and the ELO ladder land in the
    file, and that it parses back through the profile reader (excluding DEFAULT).
    Sections are asserted by their values, because their identities are generated.
    """
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
    ])
    config = tmp_path / "config" / "engines" / "eng.uci"

    result = us.seed_config("eng", engine_path="/fake/eng", config_path=str(config))
    assert result == str(config)
    assert config.exists()

    raw = configparser.ConfigParser(interpolation=None)
    raw.optionxform = str
    raw.read(str(config))
    assert raw.defaults() == {"Threads": "1"}

    # The shared profile reader excludes [DEFAULT] and lists the seeded ladder.
    names = ep.read_profile_names(str(config))
    assert names[0] == "Default"
    assert all(ep.is_profile_id(name) for name in names[1:])
    assert _profile_values(config) == [
        {"UCI_LimitStrength": "false"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1400"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1600"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1800"},
    ]


def test_seed_config_is_idempotent_and_preserves_user_edits(monkeypatch, tmp_path):
    """An existing config is never overwritten, so user edits survive.

    Seeding is lazy/first-run only; re-running it (e.g. every editor open) must
    not clobber a hand-tuned profile. Regression: probe would be called and the
    file rewritten, wiping edits.
    """
    config = tmp_path / "eng.uci"
    config.write_text("[Custom]\nUCI_Elo = 1234\n", encoding="utf-8")

    def fail_probe(path):
        raise AssertionError("probe_options must not run when config already exists")

    monkeypatch.setattr(us, "probe_options", fail_probe)
    us.seed_config("eng", engine_path="/fake/eng", config_path=str(config))

    assert config.read_text(encoding="utf-8") == "[Custom]\nUCI_Elo = 1234\n"


def test_seed_config_raises_and_writes_nothing_when_binary_missing(monkeypatch, tmp_path):
    """No binary -> EngineProbeError and no partial file left behind.

    A stray empty file would make the next call think seeding succeeded and skip
    it forever, so the failure must leave the path absent.
    """
    monkeypatch.setattr(us, "get_engine_path", lambda name: None)
    config = tmp_path / "config" / "engines" / "eng.uci"

    with pytest.raises(us.EngineProbeError):
        us.seed_config("eng", config_path=str(config))
    assert not config.exists()


def test_seed_config_creates_missing_parent_directories(monkeypatch, tmp_path):
    """Atomic write creates config/engines/ on a fresh install.

    On first run the directory tree does not exist; a regression that assumed it
    did would raise instead of seeding.
    """
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1600),
    ])
    config = tmp_path / "brand" / "new" / "tree" / "eng.uci"
    assert not config.parent.exists()

    us.seed_config("eng", engine_path="/fake/eng", config_path=str(config))
    assert config.exists()


def test_reset_config_replaces_existing_file_with_fresh_seed(monkeypatch, tmp_path):
    """Reset deletes a stale config and writes a full probe-derived ladder.

    Why: a Default-only (or hand-edited) .uci makes seed_config a no-op forever.
    Reset is the escape hatch. How regression shows: reset leaves the old file
    byte-identical (seed skipped) or drops Elo sections.
    """
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
    ])
    config = tmp_path / "eng.uci"
    config.write_text("[Default]\nUCI_LimitStrength = false\n", encoding="utf-8")

    result = us.reset_config("eng", engine_path="/fake/eng", config_path=str(config))
    assert result == str(config)
    assert _profile_values(config) == [
        {"UCI_LimitStrength": "false"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1400"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1600"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1800"},
    ]


def test_reset_config_seeds_when_absent(monkeypatch, tmp_path):
    """Reset on a missing file behaves like a fresh seed.

    Why: first-time reset (or after a failed prior probe left nothing) must still
    produce the ladder. How regression shows: reset raises or writes nothing.
    """
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("UCI_LimitStrength", "check", False),
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1600),
    ])
    config = tmp_path / "config" / "engines" / "eng.uci"

    us.reset_config("eng", engine_path="/fake/eng", config_path=str(config))
    assert _profile_values(config) == [
        {"UCI_LimitStrength": "false"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1400"},
        {"UCI_LimitStrength": "true", "UCI_Elo": "1600"},
    ]


# ---------------------------------------------------------------------------
# reconcile_config: add-only merge with the current on-disk net set
# ---------------------------------------------------------------------------


def _maia_net_paths(tmp_path, elos):
    """Create maia_weights nets for the given ELOs; return their sorted paths."""
    weights = tmp_path / "maia" / "maia_weights"
    weights.mkdir(parents=True, exist_ok=True)
    created = []
    for elo in elos:
        path = weights / f"maia-{elo}.pb.gz"
        path.write_text("net")
        created.append(str(path))
    return created


def test_reconcile_config_seeds_when_absent(monkeypatch, tmp_path):
    """With no config yet, reconcile behaves exactly like a fresh seed.

    Why: reconcile is the post-repair entry point; a first install that reaches
    it before any seed must still produce the full config, not error.

    How the regression manifests: reconcile assumes an existing file and raises
    (or writes nothing) when the config is absent, so a repaired engine ends up
    with no profiles at all.
    """
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("UCI_Elo", "spin", 1500, 1400, 1800),
    ])
    config = tmp_path / "config" / "engines" / "eng.uci"
    assert not config.exists()

    us.reconcile_config("eng", engine_path="/fake/eng", config_path=str(config))

    assert _profile_values(config) == [
        {},
        {"UCI_Elo": "1400"},
        {"UCI_Elo": "1600"},
        {"UCI_Elo": "1800"},
    ]


def test_reconcile_config_adds_missing_sections_and_preserves_edits(monkeypatch, tmp_path):
    """Nets that appeared after the first seed get sections; user edits survive.

    Why: this is the core of the "weights not listed after repair" fix. A config
    seeded while only some nets existed lacks sections for nets fetched later.
    Reconcile must add exactly the missing sections while leaving hand-tuned
    values (Threads, a chosen Default net) untouched.

    A legacy section carrying a net the ladder still wants must not be duplicated
    either: rung identity is generated, so "already present" is decided by the
    values a section sets, not by the name it happens to have.

    How the regression manifests: either the new net sections are absent (the
    fetched nets never show as strength profiles), a full reseed clobbers the
    user's Threads/Default edits, or every reconcile appends the whole ladder
    again under fresh ids.
    """
    nets = _maia_net_paths(tmp_path, [1100, 1300, 1500])
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("WeightsFile", "string", "<autodiscover>"),
    ])
    config = tmp_path / "config" / "engines" / "maia.uci"
    config.parent.mkdir(parents=True)
    # A partial seed: Threads hand-tuned to 2, Default points at a user-chosen
    # net, and only the 1100 section exists (1300/1500 were fetched later) --
    # under the name an older version gave it.
    config.write_text(
        "[DEFAULT]\nThreads = 2\n\n"
        f"[Default]\nWeightsFile = {nets[0]}\n\n"
        f"[1100 ELO]\nWeightsFile = {nets[0]}\n\n",
        encoding="utf-8",
    )

    us.reconcile_config("maia", engine_path="/fake/lc0",
                        config_path=str(config), engines_dir=str(tmp_path))

    raw = configparser.ConfigParser(interpolation=None)
    raw.optionxform = str
    raw.read(str(config))
    # The nets fetched after the first seed are now selectable strength profiles,
    # and the legacy 1100 section is still the only one carrying that net.
    by_net = {}
    for section in raw.sections():
        by_net.setdefault(raw[section].get("WeightsFile"), []).append(section)
    assert by_net[nets[1]] and all(ep.is_profile_id(s) for s in by_net[nets[1]])
    assert by_net[nets[2]] and all(ep.is_profile_id(s) for s in by_net[nets[2]])
    assert sorted(by_net[nets[0]]) == ["1100 ELO", "Default"]
    # User edits survive: reconcile never overwrites an existing value.
    assert raw.defaults() == {"Threads": "2"}
    assert raw.get("Default", "WeightsFile") == nets[0]


def test_reconcile_config_fills_empty_default_weightsfile(monkeypatch, tmp_path):
    """A net-less-seeded empty [Default] gets its WeightsFile filled in.

    Why: when Maia was first probed with zero nets, the seed wrote an empty
    [Default] (no WeightsFile). After nets arrive, playing "Default" would launch
    lc0 with no network. Reconcile must fill the missing key on the existing
    Default section, not just add new sections.

    How the regression manifests: Default stays empty after repair, so selecting
    it crashes lc0 at move time even though other ELO profiles work.
    """
    nets = _maia_net_paths(tmp_path, [1100, 1500, 1900])
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("WeightsFile", "string", "<autodiscover>"),
    ])
    config = tmp_path / "maia.uci"
    config.write_text("[DEFAULT]\nThreads = 1\n\n[Default]\n\n", encoding="utf-8")

    us.reconcile_config("maia", engine_path="/fake/lc0",
                        config_path=str(config), engines_dir=str(tmp_path))

    raw = configparser.ConfigParser(interpolation=None)
    raw.optionxform = str
    raw.read(str(config))
    # derive_sections picks the median net (of [1100,1500,1900]) for Default.
    assert raw.get("Default", "WeightsFile") == nets[1]
    seeded_nets = {
        raw[section].get("WeightsFile")
        for section in raw.sections()
        if section != "Default"
    }
    assert seeded_nets == set(nets)


def test_reconcile_config_is_noop_when_already_complete(monkeypatch, tmp_path):
    """A complete config is left byte-identical (reconcile only writes on change).

    Why: reconcile runs after every repair/top-up; when there is nothing to add
    it must not rewrite the file (which would churn user formatting and risk
    losing comments/order for no benefit).

    How the regression manifests: reconcile unconditionally rewrites, so the file
    changes on every call even when the net set already matches.
    """
    _maia_net_paths(tmp_path, [1100, 1500])
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("WeightsFile", "string", "<autodiscover>"),
    ])
    config = tmp_path / "maia.uci"
    us.reconcile_config("maia", engine_path="/fake/lc0",
                        config_path=str(config), engines_dir=str(tmp_path))
    before = config.read_text(encoding="utf-8")

    us.reconcile_config("maia", engine_path="/fake/lc0",
                        config_path=str(config), engines_dir=str(tmp_path))

    assert config.read_text(encoding="utf-8") == before

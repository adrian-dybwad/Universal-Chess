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
def test_file_level_name(path, expected):
    """File levels are named by embedded ELO when present, else the stem.

    The name is what the ELO picker shows; a wrong name would mislabel a net's
    strength.
    """
    assert us._file_level_name(path) == expected


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
    sections = dict(us.derive_sections(options, engine_name="eng"))

    assert list(sections) == ["Default", "1400 ELO", "1600 ELO", "1800 ELO"]
    assert sections["Default"] == {"UCI_LimitStrength": "false"}
    assert sections["1600 ELO"] == {"UCI_LimitStrength": "true", "UCI_Elo": "1600"}


def test_derive_sections_file_levels_take_precedence(tmp_path):
    """A file-backed engine seeds one section per net, Default at the median.

    For Maia the nets ARE the strength ladder; Default should pick a mid net so a
    fresh install is neither weakest nor strongest.
    """
    for elo in ("1100", "1500", "1900"):
        (tmp_path / f"net-{elo}.pb.gz").write_text("x")
    default = str(tmp_path / "net-1100.pb.gz")
    options = [FakeOption("WeightsFile", "string", default)]

    sections = us.derive_sections(options, engine_name="eng", engines_dir=str(tmp_path))
    as_dict = dict(sections)

    assert [name for name, _ in sections] == ["Default", "1100 ELO", "1500 ELO", "1900 ELO"]
    # Median of three sorted nets is the 1500 file.
    assert as_dict["Default"] == {"WeightsFile": str(tmp_path / "net-1500.pb.gz")}
    assert as_dict["1900 ELO"] == {"WeightsFile": str(tmp_path / "net-1900.pb.gz")}


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
# seed_config: lazy, atomic, idempotent
# ---------------------------------------------------------------------------


def test_seed_config_writes_default_section_and_sections(monkeypatch, tmp_path):
    """Seeding writes [DEFAULT] Threads plus the derived strength sections.

    The seeded file must be exactly what the engine player and pickers read;
    this asserts the DEFAULT engine-wide block and the ELO ladder land in the
    file, and that it parses back through the profile reader (excluding DEFAULT).
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
    assert raw.get("1600 ELO", "UCI_Elo") == "1600"

    # The shared profile reader excludes [DEFAULT] and lists the seeded ladder.
    assert ep.read_profile_names(str(config)) == ["Default", "1400 ELO", "1600 ELO", "1800 ELO"]


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
    assert ep.read_profile_names(str(config)) == [
        "Default", "1400 ELO", "1600 ELO", "1800 ELO",
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
    assert ep.read_profile_names(str(config)) == ["Default", "1400 ELO", "1600 ELO"]


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

    assert ep.read_profile_names(str(config)) == ["Default", "1400 ELO", "1600 ELO", "1800 ELO"]


def test_reconcile_config_adds_missing_sections_and_preserves_edits(monkeypatch, tmp_path):
    """Nets that appeared after the first seed get sections; user edits survive.

    Why: this is the core of the "weights not listed after repair" fix. A config
    seeded while only some nets existed lacks sections for nets fetched later.
    Reconcile must add exactly the missing sections while leaving hand-tuned
    values (Threads, a chosen Default net) untouched.

    How the regression manifests: either the new net sections are absent (the
    fetched nets never show as strength profiles) or a full reseed clobbers the
    user's Threads/Default edits.
    """
    nets = _maia_net_paths(tmp_path, [1100, 1300, 1500])
    monkeypatch.setattr(us, "probe_options", lambda path: [
        FakeOption("WeightsFile", "string", "<autodiscover>"),
    ])
    config = tmp_path / "config" / "engines" / "maia.uci"
    config.parent.mkdir(parents=True)
    # A partial seed: Threads hand-tuned to 2, Default points at a user-chosen
    # net, and only the 1100 section exists (1300/1500 were fetched later).
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
    # The nets fetched after the first seed are now selectable strength profiles.
    assert raw.get("1300 ELO", "WeightsFile") == nets[1]
    assert raw.get("1500 ELO", "WeightsFile") == nets[2]
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
    assert raw.has_section("1100 ELO")
    assert raw.has_section("1900 ELO")


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

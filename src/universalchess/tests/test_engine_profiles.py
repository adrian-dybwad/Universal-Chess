"""Tests for engine option profile read/validate/write logic.

Why these tests exist
---------------------
The profile editor writes UCI ``setoption`` settings into the same ``.uci`` file
the engine player loads at game start. Two engine behaviours make correctness
fragile and motivate strict guarding here (see ``engine_profiles`` module
docstring): many engines silently ignore unrecognized option names and accept
out-of-range values without clamping. So a bad key or value would not error --
it would just make the engine play wrong. These tests pin:

* section-local reads (engine-wide ``[DEFAULT]`` Hash/Threads must not leak into
  a profile's editable values, and must survive writes untouched);
* schema validation against a supplied (probed) schema rejects unknown keys and
  out-of-range/ill-typed values rather than writing them;
* writes are wholesale-per-section and preserve every other profile + DEFAULT;
* the written file stays loadable by the runtime's standard configparser with
  DEFAULT inheritance intact.

The schema here is built directly from :class:`ProfileField`/:class:`ProfileGroup`
(the same shapes ``services.uci_schema`` produces by probing a binary), so these
tests cover the generic behaviour independent of any particular engine.
"""

import configparser

import pytest

from universalchess.services import engine_profiles as ep


# A representative probed-style schema: strength controls plus a few advanced
# options spanning every field type (bool, bounded int, string combo, file combo
# with a free-text escape hatch, and free text). Mirrors what uci_schema.build_groups
# emits for a real engine.
GROUPS = (
    ep.ProfileGroup("strength", "Strength", (
        ep.ProfileField("UCI_LimitStrength", "Limit strength", "bool", False),
        ep.ProfileField("UCI_Elo", "ELO", "int", 2800, 800, 2800),
    )),
    ep.ProfileGroup("advanced", "Advanced", (
        ep.ProfileField("OwnAttack", "Own attack", "int", 100, 0, 500),
        ep.ProfileField("Contempt", "Contempt", "int", 0, -500, 500),
        ep.ProfileField(
            "Style", "Style", "select", "Normal",
            options=(("Solid", "Solid"), ("Normal", "Normal"), ("Aggressive", "Aggressive")),
        ),
        ep.ProfileField(
            "WeightsFile", "Weights", "select", "",
            options=(("/nets/a.pb", "a.pb"), ("/nets/b.pb", "b.pb")),
            allow_custom=True,
        ),
        ep.ProfileField("Description", "Description", "text", ""),
    )),
)


# A compact but representative config: a DEFAULT block with engine-wide settings,
# the special "Default" profile, an ELO profile, and a personality profile that
# sets a couple of options.
SAMPLE_UCI = """\
[DEFAULT]
Description = Personality engine
Hash = 16
Threads = 2

[Default]
Description = Maximum strength
UCI_LimitStrength = false

[1200 ELO]
UCI_LimitStrength = true
UCI_Elo = 1200

[Attacker]
Description = Aggressive attacking style
OwnAttack = 125
Style = Aggressive
"""


@pytest.fixture
def uci_file(tmp_path):
    """Write the sample config and return its path."""
    path = tmp_path / "engine.uci"
    path.write_text(SAMPLE_UCI, encoding="utf-8")
    return path


@pytest.fixture
def groups():
    return GROUPS


# ---------------------------------------------------------------------------
# Schema serialization
# ---------------------------------------------------------------------------


def test_schema_keys_are_unique(groups):
    """Duplicate keys would make the field index ambiguous and validation wrong.

    A duplicate would silently shadow one field's range/type with another's,
    so an out-of-range value could pass under the wrong field's bounds.
    """
    keys = [f.key for g in groups for f in g.fields]
    assert len(keys) == len(set(keys))


def test_schema_to_json_exposes_bounds_and_options(groups):
    """Frontend form depends on min/max for ints and string options for selects.

    Missing bounds/options would render unconstrained inputs, letting users
    submit values the engine mis-applies (the regression this guards). Combo
    options must serialize as string value/label pairs (not integers), and a
    file option must carry allow_custom so the UI offers the free-text path.
    """
    payload = ep.schema_to_json(groups)
    by_key = {f["key"]: f for grp in payload for f in grp["fields"]}

    assert by_key["UCI_Elo"]["min"] == 800
    assert by_key["UCI_Elo"]["max"] == 2800
    assert by_key["Style"]["options"] == [
        {"value": "Solid", "label": "Solid"},
        {"value": "Normal", "label": "Normal"},
        {"value": "Aggressive", "label": "Aggressive"},
    ]
    assert by_key["Style"]["allow_custom"] is False
    assert by_key["WeightsFile"]["allow_custom"] is True
    # Description is a text field with neither bounds nor options.
    assert by_key["Description"]["type"] == "text"
    assert "min" not in by_key["Description"]
    assert "options" not in by_key["Description"]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_read_profiles_excludes_default_and_keeps_section_local_values(uci_file):
    """Profiles list excludes [DEFAULT]; values hold only section-local keys.

    The core regression: configparser's DEFAULT inheritance would otherwise
    merge Hash/Threads into every profile's values, so the editor would show
    (and re-write) engine-wide settings into each section. The assertions check
    both the exact profile set and that Hash/Threads do NOT appear in a profile.
    """
    profiles = ep.read_profiles(str(uci_file))
    by_name = {p["name"]: p["values"] for p in profiles}

    assert set(by_name) == {"Default", "1200 ELO", "Attacker"}
    assert by_name["Attacker"] == {
        "Description": "Aggressive attacking style",
        "OwnAttack": "125",
        "Style": "Aggressive",
    }
    assert "Hash" not in by_name["1200 ELO"]
    assert by_name["1200 ELO"] == {"UCI_LimitStrength": "true", "UCI_Elo": "1200"}


def test_read_profiles_falls_back_to_defaults_when_config_absent(tmp_path):
    """A never-edited install reads a provided fallback file when config is absent.

    Seeding normally writes the config, but the reader still supports an explicit
    defaults path (used by callers that seed lazily); without it the editor would
    show an empty list when only the fallback exists.
    """
    missing = tmp_path / "config" / "engine.uci"
    defaults = tmp_path / "defaults" / "engine.uci"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(SAMPLE_UCI, encoding="utf-8")

    profiles = ep.read_profiles(str(missing), str(defaults))
    assert {p["name"] for p in profiles} == {"Default", "1200 ELO", "Attacker"}


def test_read_profile_names_lists_each_profile_once_in_order(uci_file):
    """Profile names are listed in file order with "Default" appearing once.

    Why this exists: the on-device ELO picker and the web editor must agree on
    the profile list. A seeded .uci carries BOTH an engine-wide [DEFAULT] section
    and an ordinary [Default] profile; a naive reader that seeded "Default"
    itself and also appended the file's [Default] would show it twice. This pins
    the shared reader to the DEFAULT-excluded, deduplicated, ordered list.

    How the regression manifests: "Default" appears twice (or out of file
    order), reintroducing the duplicate the shared reader exists to prevent.
    """
    names = ep.read_profile_names(str(uci_file))

    assert names == ["Default", "1200 ELO", "Attacker"]
    assert names.count("Default") == 1


# ---------------------------------------------------------------------------
# Strength picker labels (Default -> "Unlimited" only when uncapped)
# ---------------------------------------------------------------------------


def test_strength_level_choices_labels_uncapped_default_as_unlimited(uci_file):
    """An ELO engine's uncapped Default is shown as "Unlimited" without changing
    its stored value.

    Why this exists: for a UCI_Elo engine, [Default] sets
    UCI_LimitStrength=false, so it plays uncapped/full strength -- stronger than
    any numbered rung -- and the word "Default" misled users. The relabel must be
    display-only: the persisted value stays "Default" so existing player configs
    and the .uci section keep matching.

    How the regression manifests: the Default row's label reverts to "Default"
    (users confused again) or its value changes away from "Default" (stored elo
    no longer resolves to the [Default] section).
    """
    choices = ep.strength_level_choices(str(uci_file))

    assert choices == [
        {"value": "Default", "label": ep.UNLIMITED_LABEL},
        {"value": "1200 ELO", "label": "1200 ELO"},
        {"value": "Attacker", "label": "Attacker"},
    ]


def test_strength_level_choices_keeps_default_name_when_not_uncapped(tmp_path):
    """A file-model Default (no cap toggle, e.g. Maia) keeps the name "Default".

    Why this exists: "Unlimited" is only truthful when Default disables the cap.
    A file-selector engine's [Default] merely picks a net and carries no
    UCI_LimitStrength=false, so relabelling it "Unlimited" would be a lie. The
    relabel is keyed off the cap being off, not off the name.

    How the regression manifests: a name-based relabel would mark this Default
    "Unlimited" too, mislabelling a non-full-strength default.
    """
    path = tmp_path / "maia.uci"
    path.write_text(
        "[Default]\nWeightsFile = /nets/1500.pb\n\n"
        "[1500 ELO]\nWeightsFile = /nets/1500.pb\n",
        encoding="utf-8",
    )

    choices = ep.strength_level_choices(str(path))

    assert choices == [
        {"value": "Default", "label": "Default"},
        {"value": "1500 ELO", "label": "1500 ELO"},
    ]


def test_strength_level_choices_always_offers_default(tmp_path):
    """A config without a Default section still yields a Default row (first).

    Why this exists: the picker must always offer Default so the stored default
    setting resolves even for a sparse/edited config. When no Default section
    exists there is no cap signal, so it stays labelled "Default", not
    "Unlimited".

    How the regression manifests: the picker omits Default and the stored
    default value has no matching row (blank/again-unselectable strength).
    """
    path = tmp_path / "sparse.uci"
    path.write_text("[1800 ELO]\nUCI_Elo = 1800\n", encoding="utf-8")

    choices = ep.strength_level_choices(str(path))

    assert choices == [
        {"value": "Default", "label": "Default"},
        {"value": "1800 ELO", "label": "1800 ELO"},
    ]


# ---------------------------------------------------------------------------
# Validation against a supplied (probed) schema
# ---------------------------------------------------------------------------


def test_validate_coerces_each_type(groups):
    """Valid values coerce to the .uci string forms the engine expects.

    Bool -> 'true'/'false', int -> decimal string, combo/file/text -> trimmed
    string. A wrong coercion (e.g. Python 'True') would be written and misread
    by the engine.
    """
    out = ep.validate_profile_values(groups, {
        "UCI_LimitStrength": True,
        "UCI_Elo": 1500,
        "Style": "Aggressive",
        "WeightsFile": "/some/custom/net.pb",  # allow_custom accepts a new path
        "OwnAttack": "300",
        "Description": "  My style  ",
    })
    assert out == {
        "UCI_LimitStrength": "true",
        "UCI_Elo": "1500",
        "Style": "Aggressive",
        "WeightsFile": "/some/custom/net.pb",
        "OwnAttack": "300",
        "Description": "My style",
    }


def test_validate_rejects_unknown_key(groups):
    """Unknown option names are rejected, not written.

    Engines silently ignore unknown options, so writing one would create a
    dead, misleading entry; the error names the offending key for the UI.
    """
    with pytest.raises(ep.ProfileValidationError, match="NotAReal"):
        ep.validate_profile_values(groups, {"NotAReal": 1})


@pytest.mark.parametrize("key,value", [
    ("UCI_Elo", 799),    # below min 800
    ("UCI_Elo", 2801),   # above max 2800
    ("OwnAttack", 501),  # above max 500
    ("Contempt", -501),  # below min -500
])
def test_validate_rejects_out_of_range_int(groups, key, value):
    """Out-of-range ints are rejected because the engine does not clamp them.

    Each parameter sits one step past a real bound (the probed min/max); a
    regression that dropped range checks would let these through and corrupt
    evaluation/strength.
    """
    with pytest.raises(ep.ProfileValidationError, match=key):
        ep.validate_profile_values(groups, {key: value})


def test_validate_rejects_non_numeric_and_bool_as_int(groups):
    """Non-numeric strings and booleans are not valid ints.

    bool is an int subclass in Python; accepting it would let 'true' silently
    become 1 for a numeric option. Both must error.
    """
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"UCI_Elo": "fast"})
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"UCI_Elo": True})


def test_validate_rejects_invalid_select_and_bad_bool(groups):
    """Combo values must be one of the enum; bools must be boolean-like.

    A constrained select (no allow_custom) rejects a value outside its options,
    while a bad boolean-like value is rejected too.
    """
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"Style": "Wild"})
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"UCI_LimitStrength": "maybe"})


def test_validate_allows_custom_value_only_for_allow_custom_select(groups):
    """A file option (allow_custom) accepts an arbitrary path; a plain combo does not.

    This is the file-picker escape hatch: WeightsFile's enumerated list is a
    convenience, so a path outside it is valid; Style has a fixed set, so an
    off-list value must be rejected. A regression that treated all selects alike
    would either reject valid custom nets or accept invalid styles.
    """
    out = ep.validate_profile_values(groups, {"WeightsFile": "/elsewhere/x.pb"})
    assert out == {"WeightsFile": "/elsewhere/x.pb"}
    with pytest.raises(ep.ProfileValidationError, match="Style"):
        ep.validate_profile_values(groups, {"Style": "/elsewhere/x.pb"})


def test_validate_rejects_multiline_text(groups):
    """Description must be a single line so it cannot inject extra INI lines.

    A newline in a value would split into a bogus second key on write,
    potentially smuggling an unintended option into the section.
    """
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"Description": "a\nThreads = 99"})


# ---------------------------------------------------------------------------
# Value-returning validation (used by HTTP handlers to avoid exposing a caught
# exception's text in the response -- CodeQL py/stack-trace-exposure)
# ---------------------------------------------------------------------------


def test_validation_error_returns_none_for_valid_values(groups):
    """validation_error returns None when every value passes the schema."""
    assert ep.validation_error(groups, {
        "UCI_Elo": 1500,
        "UCI_LimitStrength": True,
        "Description": "  Sharp  ",
    }) is None


@pytest.mark.parametrize("values,needle", [
    ({"NotAReal": 1}, "NotAReal"),       # unknown key echoed back
    ({"UCI_Elo": 2801}, "UCI_Elo"),      # above max
    ({"UCI_Elo": "fast"}, "UCI_Elo"),    # non-numeric
    ({"Style": "Wild"}, "Style"),        # invalid combo
    ({"UCI_LimitStrength": "maybe"}, "UCI_LimitStrength"),  # bad bool
    ("not-a-dict", "object"),            # non-dict payload
])
def test_validation_error_returns_message_naming_the_problem(groups, values, needle):
    """validation_error returns a message (not None) that names the offending field.

    Why: this is the exact message the editor shows. It must match what the
    raising path produces and identify the field/problem.
    """
    message = ep.validation_error(groups, values)
    assert message is not None
    assert needle in message


def test_validation_error_matches_raising_path(groups):
    """The value-based and raising entry points share one message source."""
    bad = {"UCI_Elo": 99999}
    with pytest.raises(ep.ProfileValidationError) as exc_info:
        ep.validate_profile_values(groups, bad)
    assert ep.validation_error(groups, bad) == str(exc_info.value)


def test_delete_blocked_reason_blocks_default_and_allows_others():
    """delete_blocked_reason flags the reserved DEFAULT section, else None."""
    assert ep.delete_blocked_reason("DEFAULT") is not None
    assert ep.delete_blocked_reason("Attacker") is None


# ---------------------------------------------------------------------------
# Profile name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Default", "1200 ELO", "Club Player", "Tal"])
def test_valid_profile_names_accepted(name):
    """Real profile names (with spaces/digits) are accepted."""
    assert ep.is_valid_profile_name(name) is True


@pytest.mark.parametrize("name", [
    "",            # empty
    "  ",          # whitespace only
    " Tal",        # leading space (would be trimmed by configparser -> mismatch)
    "DEFAULT",     # reserved engine defaults section
    "a[b]",        # brackets break the INI header
    "x\ny",        # newline
    "x" * 65,      # too long
])
def test_invalid_profile_names_rejected(name):
    """Names that break the INI file or collide with DEFAULT are rejected."""
    assert ep.is_valid_profile_name(name) is False


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_write_new_profile_preserves_others_and_default(uci_file, groups):
    """Adding a profile leaves every existing profile and [DEFAULT] intact.

    A regression that rewrote the file from only the new section would wipe the
    catalog; asserting the full resulting name set and DEFAULT contents catches
    both deletion of others and corruption of engine-wide settings.
    """
    ep.write_profile(str(uci_file), "Tactical", {
        "Description": "Sharp",
        "OwnAttack": 140,
        "Style": "Aggressive",
    }, groups)

    profiles = {p["name"]: p["values"] for p in ep.read_profiles(str(uci_file))}
    assert set(profiles) == {"Default", "1200 ELO", "Attacker", "Tactical"}
    assert profiles["Tactical"] == {
        "Description": "Sharp",
        "OwnAttack": "140",
        "Style": "Aggressive",
    }
    # Pre-existing profile untouched.
    assert profiles["Attacker"]["OwnAttack"] == "125"

    # [DEFAULT] engine-wide settings preserved verbatim.
    raw = configparser.ConfigParser()
    raw.optionxform = str
    raw.read(str(uci_file))
    assert raw.defaults() == {
        "Description": "Personality engine",
        "Hash": "16",
        "Threads": "2",
    }


def test_write_replaces_section_wholesale(uci_file, groups):
    """Editing a profile replaces its keys entirely (removed keys disappear).

    The editor submits the complete desired set; a regression that merged into
    the old section would leave stale keys (here 'Style') that the user removed.
    """
    ep.write_profile(str(uci_file), "Attacker", {
        "OwnAttack": 200,
    }, groups)
    profiles = {p["name"]: p["values"] for p in ep.read_profiles(str(uci_file))}
    assert profiles["Attacker"] == {"OwnAttack": "200"}


def test_written_file_is_loadable_by_runtime_with_default_inheritance(uci_file, groups):
    """The runtime reads sections WITH DEFAULT inheritance; that must still work.

    players.engine loads options via a standard ConfigParser (DEFAULT merges in).
    This asserts a freshly written profile still inherits Hash/Threads, i.e. the
    engine receives both the profile's options and the engine-wide settings.
    """
    ep.write_profile(str(uci_file), "Tactical", {"OwnAttack": 140}, groups)

    runtime = configparser.ConfigParser()
    runtime.optionxform = str
    runtime.read(str(uci_file))
    merged = dict(runtime.items("Tactical"))
    assert merged["OwnAttack"] == "140"
    assert merged["Hash"] == "16"      # inherited from [DEFAULT]
    assert merged["Threads"] == "2"


def test_write_seeds_from_defaults_when_config_absent(tmp_path, groups):
    """First edit on a fresh install seeds from a provided defaults file.

    Without seeding, the first write would create a file containing only the
    edited profile, silently discarding the other profiles.
    """
    config = tmp_path / "config" / "engine.uci"
    defaults = tmp_path / "defaults" / "engine.uci"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(SAMPLE_UCI, encoding="utf-8")

    ep.write_profile(str(config), "Tactical", {"OwnAttack": 140}, groups,
                     defaults_path=str(defaults))

    profiles = {p["name"] for p in ep.read_profiles(str(config))}
    assert profiles == {"Default", "1200 ELO", "Attacker", "Tactical"}


def test_write_invalid_value_does_not_create_or_modify_file(tmp_path, groups):
    """A rejected value must not leave a partial/empty file behind.

    Validation runs before any file I/O; this guards that ordering so a bad
    request cannot truncate or seed the config as a side effect.
    """
    config = tmp_path / "engine.uci"
    with pytest.raises(ep.ProfileValidationError):
        ep.write_profile(str(config), "Tactical", {"UCI_Elo": 99999}, groups)
    assert not config.exists()


def test_write_invalid_name_raises(uci_file, groups):
    """Invalid profile names are rejected before writing."""
    with pytest.raises(ep.ProfileValidationError):
        ep.write_profile(str(uci_file), "DEFAULT", {"OwnAttack": 100}, groups)


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def test_delete_removes_only_the_named_profile(uci_file):
    """Deleting one profile leaves the rest and [DEFAULT] intact."""
    removed = ep.delete_profile(str(uci_file), "Attacker")
    assert removed is True
    profiles = {p["name"] for p in ep.read_profiles(str(uci_file))}
    assert profiles == {"Default", "1200 ELO"}


def test_delete_missing_profile_returns_false(uci_file):
    """Deleting a non-existent profile is a no-op reported as False."""
    assert ep.delete_profile(str(uci_file), "Nope") is False


def test_delete_default_section_refused(uci_file):
    """Refuse to delete the reserved [DEFAULT] section."""
    with pytest.raises(ep.ProfileValidationError):
        ep.delete_profile(str(uci_file), "DEFAULT")

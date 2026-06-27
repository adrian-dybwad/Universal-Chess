"""Tests for engine personality profile read/validate/write logic.

Why these tests exist
---------------------
The profile editor writes UCI ``setoption`` settings into the same ``.uci`` file
the engine player loads at game start. Two engine behaviours make correctness
fragile and motivate strict guarding here (see ``engine_profiles`` module
docstring): Rodent IV silently ignores unrecognized option names and accepts
out-of-range values without clamping. So a bad key or value would not error --
it would just make the engine play wrong. These tests pin:

* section-local reads (engine-wide ``[DEFAULT]`` Hash/Threads must not leak into
  a profile's editable values, and must survive writes untouched);
* schema validation rejects unknown keys and out-of-range/ill-typed values
  rather than writing them;
* writes are wholesale-per-section and preserve every other profile + DEFAULT;
* the written file stays loadable by the runtime's standard configparser with
  DEFAULT inheritance intact.
"""

import configparser

import pytest

from universalchess.services import engine_profiles as ep


# A compact but representative config: a DEFAULT block with engine-wide settings,
# the special "Default" profile, an ELO profile, and a personality profile that
# sets eval weights. Mirrors the real defaults/engines/rodentIV.uci shape.
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
OppAttack = 100
Space = 100
"""


@pytest.fixture
def uci_file(tmp_path):
    """Write the sample config and return its path."""
    path = tmp_path / "rodentIV.uci"
    path.write_text(SAMPLE_UCI, encoding="utf-8")
    return path


@pytest.fixture
def groups():
    return ep.get_schema("rodentIV")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_registry_only_contains_editable_engines():
    """Engines without a schema return None so the UI hides the editor.

    If an unmapped engine leaked a schema, the UI would offer a meaningless
    editor for an engine whose .uci has no tunable parameters.
    """
    assert ep.get_schema("rodentIV") is not None
    assert ep.get_schema("stockfish") is None


def test_schema_keys_are_unique(groups):
    """Duplicate keys would make the field index ambiguous and validation wrong.

    A duplicate would silently shadow one field's range/type with another's,
    so an out-of-range value could pass under the wrong field's bounds.
    """
    keys = [f.key for g in groups for f in g.fields]
    assert len(keys) == len(set(keys))


def test_schema_to_json_exposes_bounds_and_options(groups):
    """Frontend form depends on min/max for ints and options for selects.

    Missing bounds/options would render unconstrained inputs, letting users
    submit values the engine mis-applies (the regression this guards).
    """
    payload = ep.schema_to_json(groups)
    by_key = {f["key"]: f for grp in payload for f in grp["fields"]}

    assert by_key["UCI_Elo"]["min"] == 800
    assert by_key["UCI_Elo"]["max"] == 2800
    assert by_key["PrimaryPstStyle"]["options"] == [
        {"value": 0, "label": "Quirky"},
        {"value": 1, "label": "Classic"},
        {"value": 2, "label": "Normal"},
        {"value": 3, "label": "Blunt"},
        {"value": 4, "label": "Forward"},
    ]
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
    # Attacker's local keys only -- no inherited Hash/Threads/DEFAULT Description.
    assert by_name["Attacker"] == {
        "Description": "Aggressive attacking style",
        "OwnAttack": "125",
        "OppAttack": "100",
        "Space": "100",
    }
    assert "Hash" not in by_name["1200 ELO"]
    assert by_name["1200 ELO"] == {"UCI_LimitStrength": "true", "UCI_Elo": "1200"}


def test_read_profiles_falls_back_to_defaults_when_config_absent(tmp_path):
    """A never-edited install reads the packaged defaults file.

    Without the fallback the editor would show an empty list on a fresh install
    even though the curated profiles ship in defaults/.
    """
    missing = tmp_path / "config" / "rodentIV.uci"
    defaults = tmp_path / "defaults" / "rodentIV.uci"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(SAMPLE_UCI, encoding="utf-8")

    profiles = ep.read_profiles(str(missing), str(defaults))
    assert {p["name"] for p in profiles} == {"Default", "1200 ELO", "Attacker"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_coerces_each_type(groups):
    """Valid values coerce to the .uci string forms the engine expects.

    Bool -> 'true'/'false', int/select -> decimal string. A wrong coercion
    (e.g. Python 'True') would be written and misread by the engine.
    """
    out = ep.validate_profile_values(groups, {
        "UCI_LimitStrength": True,
        "UCI_Elo": 1500,
        "PrimaryPstStyle": 4,
        "OwnAttack": "300",
        "Description": "  My style  ",
    })
    assert out == {
        "UCI_LimitStrength": "true",
        "UCI_Elo": "1500",
        "PrimaryPstStyle": "4",
        "OwnAttack": "300",
        "Description": "My style",
    }


def test_validate_rejects_unknown_key(groups):
    """Unknown option names are rejected, not written.

    Rodent silently ignores unknown options, so writing one would create a
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

    Each parameter sits one step past a real bound; a regression that dropped
    range checks would let these through and corrupt evaluation/strength.
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
    """Select values must be one of the enum; bools must be boolean-like."""
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"PrimaryPstStyle": 9})
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"UCI_LimitStrength": "maybe"})


def test_validate_rejects_multiline_text(groups):
    """Description must be a single line so it cannot inject extra INI lines.

    A newline in a value would split into a bogus second key on write,
    potentially smuggling an unintended option into the section.
    """
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"Description": "a\nThreads = 99"})


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
    """Names that break the INI file or collide with DEFAULT are rejected.

    'DEFAULT' is special: editing it as a profile would clobber engine-wide
    Hash/Threads. Brackets/newlines would corrupt the file structure.
    """
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
        "PrimaryPstStyle": 2,
    }, groups)

    profiles = {p["name"]: p["values"] for p in ep.read_profiles(str(uci_file))}
    assert set(profiles) == {"Default", "1200 ELO", "Attacker", "Tactical"}
    assert profiles["Tactical"] == {
        "Description": "Sharp",
        "OwnAttack": "140",
        "PrimaryPstStyle": "2",
    }
    # Pre-existing profile untouched.
    assert profiles["Attacker"]["OwnAttack"] == "125"

    # [DEFAULT] engine-wide settings preserved verbatim (read raw, with the
    # runtime's standard DEFAULT semantics).
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
    the old section would leave stale keys (here 'Space') that the user removed.
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
    """First edit on a fresh install seeds from defaults, keeping the catalog.

    Without seeding, the first write would create a file containing only the
    edited profile, silently discarding the shipped profiles.
    """
    config = tmp_path / "config" / "rodentIV.uci"
    defaults = tmp_path / "defaults" / "rodentIV.uci"
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
    config = tmp_path / "rodentIV.uci"
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
    """Deleting a non-existent profile is a no-op reported as False.

    Lets the endpoint return 404 instead of pretending success.
    """
    assert ep.delete_profile(str(uci_file), "Nope") is False


def test_delete_default_section_refused(uci_file):
    """Refuse to delete the reserved [DEFAULT] section."""
    with pytest.raises(ep.ProfileValidationError):
        ep.delete_profile(str(uci_file), "DEFAULT")

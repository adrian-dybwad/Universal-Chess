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
from universalchess.services import profile_labels as pl


# A representative probed-style schema: strength controls plus a few advanced
# options spanning every field type (bool, bounded int, string combo, file combo
# with a free-text escape hatch, and free text). Mirrors what uci_schema.build_groups
# emits for a real engine.
GROUPS = (
    ep.ProfileGroup("strength", "Strength", (
        ep.ProfileField("UCI_LimitStrength", "Limit strength", "bool", False),
        # requires/unit as uci_schema's option registry fills them in: the Elo is
        # ignored while the cap is off, and a bare number does not say what it
        # measures.
        ep.ProfileField(
            "UCI_Elo", "ELO", "int", 2800, 800, 2800,
            requires="UCI_LimitStrength", unit="ELO",
        ),
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
# Profile labels: projected from values, never read off the identity
# ---------------------------------------------------------------------------


@pytest.fixture
def projection():
    """The projection a UCI_Elo engine with a net selector yields."""
    fields = tuple(field for group in GROUPS for field in group.fields)
    return pl.LabelProjection(
        keys=("WeightsFile", "UCI_Elo"), fields=fields, fallback=("UCI_Elo",)
    )


def test_a_profiles_label_comes_from_its_values(projection):
    """The label is composed from the options, so it cannot contradict them.

    Why this exists: the section name was the label as well as the identity, so a
    rung named ``1000 ELO`` whose ``UCI_Elo`` had been edited to 1400 went on
    claiming 1000, and the only remedy was renaming the section and every setting
    that referred to it. Identities are generated now and the Elo is stored once.

    How a regression manifests: the phrase reads the identity again (so an
    edited Elo stops showing), or a capped profile and an uncapped one label the
    same, hiding which one plays full strength.
    """
    capped = {"UCI_LimitStrength": "true", "UCI_Elo": "1400"}
    assert ep.profile_phrase(capped, projection) == "1400 ELO"
    # Gated off: the engine ignores the Elo, so the label must not advertise it.
    uncapped = {"UCI_LimitStrength": "false", "UCI_Elo": "1400"}
    assert ep.profile_phrase(uncapped, projection) == ep.UNLIMITED_LABEL
    # Two axes at once, which naming a section could not express.
    both = {"UCI_LimitStrength": "true", "UCI_Elo": "1700", "WeightsFile": "/n/Defender.txt"}
    assert ep.profile_phrase(both, projection) == "Defender: 1700 ELO"
    # Nothing on any labelled axis, and no cap to lift: the caller decides.
    assert ep.profile_phrase({"Contempt": "20"}, projection) == ""


def test_a_users_name_outranks_the_projection(projection):
    # A profile the user named is called that, whatever its values project to:
    # the user has said what it is. A regression shows the composed terms
    # instead, so a renamed profile appears under a name nobody chose.
    values = {"Name": "Club Player", "UCI_LimitStrength": "true", "UCI_Elo": "1600"}
    assert ep.profile_phrase(values, projection) == "Club Player"


def test_profile_rows_separate_identity_from_name_and_label(tmp_path, projection):
    """Each row carries the id, the user name, and the projected label apart.

    Why this exists: the four roles the section name used to play (identity,
    stored reference, URL segment, label) are what made every rename a
    referential-integrity problem. The editor and the pickers now address a
    profile by ``id`` and show ``label``.

    How a regression manifests: ``id`` carries a label (so an edit renames the
    profile and dangles the settings pointing at it), or ``label`` falls back to
    the opaque id where a phrase was available.
    """
    path = tmp_path / "eng.uci"
    path.write_text(
        "[Default]\nUCI_LimitStrength = false\n\n"
        "[Profile-a1b2c3]\nUCI_LimitStrength = true\nUCI_Elo = 1400\n\n"
        "[Profile-d4e5f6]\nName = Club Player\nUCI_LimitStrength = true\nUCI_Elo = 1600\n\n"
        "[Profile-999999]\nContempt = 20\n",
        encoding="utf-8",
    )

    rows = ep.profile_rows(str(path), projection)

    assert rows == [
        {
            "id": "Default",
            "name": "",
            "label": f"Default ({ep.UNLIMITED_LABEL})",
            "values": {"UCI_LimitStrength": "false"},
        },
        {
            "id": "Profile-a1b2c3",
            "name": "",
            "label": "1400 ELO",
            "values": {"UCI_LimitStrength": "true", "UCI_Elo": "1400"},
        },
        {
            "id": "Profile-d4e5f6",
            "name": "Club Player",
            "label": "Club Player",
            "values": {
                "Name": "Club Player",
                "UCI_LimitStrength": "true",
                "UCI_Elo": "1600",
            },
        },
        # States no strength and carries no name: the id is all there is to show.
        {
            "id": "Profile-999999",
            "name": "",
            "label": "Profile-999999",
            "values": {"Contempt": "20"},
        },
    ]


def test_the_picker_rows_store_the_id_and_show_the_label(tmp_path, projection):
    """Picker rows pair the stored id with the projected label.

    Why this exists: ``value`` is written into the player's settings, so it must
    be the identity and not the label -- storing a label was what let a rename
    strand the setting. How a regression manifests: ``value`` becomes the
    displayed text, and the stored strength stops resolving after any edit.
    """
    path = tmp_path / "eng.uci"
    path.write_text(
        "[Default]\nUCI_LimitStrength = false\n\n"
        "[Profile-a1b2c3]\nUCI_LimitStrength = true\nUCI_Elo = 1400\n",
        encoding="utf-8",
    )

    assert ep.strength_level_choices(str(path), projection) == [
        {"value": "Default", "label": f"Default ({ep.UNLIMITED_LABEL})"},
        {"value": "Profile-a1b2c3", "label": "1400 ELO"},
    ]


def test_a_file_selected_default_is_labelled_with_the_file_it_picks(
    tmp_path, projection
):
    """A Default that selects a net is labelled with that net, not "Unlimited".

    Why this exists: "Unlimited" is only truthful where the engine has a cap and
    Default lifts it. Maia's Default merely picks a net -- the middle rung of a
    rated ladder -- so it plays a concrete strength, and a picker that called it
    Unlimited would claim the opposite of what it does.

    How a regression manifests: the file-selecting Default reads "Unlimited"
    (overstating it) or a bare "Default" (hiding the rating it plays).
    """
    path = tmp_path / "maia.uci"
    path.write_text(
        "[Default]\nWeightsFile = /nets/maia-1500.pb\n\n"
        "[Profile-a1b2c3]\nWeightsFile = /nets/maia-1300.pb\n",
        encoding="utf-8",
    )

    assert ep.strength_level_choices(str(path), projection) == [
        {"value": "Default", "label": "Default (1500 ELO)"},
        {"value": "Profile-a1b2c3", "label": "1300 ELO"},
    ]


def test_a_config_without_a_default_still_offers_one_first(tmp_path, projection):
    """A Default row is always present, first, however sparse the config.

    Why this exists: Default is the stored strength of a freshly configured
    player, so a list without it cannot render the current selection -- the
    picker would show a blank strength for a slot that was never edited. With no
    Default section there is no cap signal, so it stays the bare word.

    How a regression manifests: the picker omits Default, or lists it after the
    rungs where it reads as one of them.
    """
    path = tmp_path / "sparse.uci"
    path.write_text(
        "[Profile-a1b2c3]\nUCI_LimitStrength = true\nUCI_Elo = 1800\n", encoding="utf-8"
    )

    assert ep.strength_level_choices(str(path), projection) == [
        {"value": "Default", "label": "Default"},
        {"value": "Profile-a1b2c3", "label": "1800 ELO"},
    ]


# ---------------------------------------------------------------------------
# strength_section_display: stored reference -> shown strength (card / PGN)
# ---------------------------------------------------------------------------


def test_the_card_shows_what_the_stored_strength_resolves_to(uci_file, projection):
    """The shown strength is the phrase alone, with no "Default (...)" wrapper.

    Why this exists: the card and the PGN compose the strength into a player name
    ("Maia (1500 ELO)"), so the picker's parenthetical would nest. An uncapped
    Default reads "Unlimited" rather than the raw section, which is what the
    stored value would otherwise print.

    How a regression manifests: the card shows a bare "Default" (saying nothing
    about the strength played) or "Default (Unlimited)" (nested parentheses once
    composed into the name).
    """
    assert (
        ep.strength_section_display(str(uci_file), "Default", projection)
        == ep.UNLIMITED_LABEL
    )
    assert (
        ep.strength_section_display(str(uci_file), "1200 ELO", projection)
        == "1200 ELO"
    )


def test_a_strength_that_resolves_to_nothing_is_shown_as_stored(tmp_path, projection):
    """An unresolvable reference prints itself rather than an invented strength.

    Why this exists: a reference whose profile is gone is a fault to be seen and
    repaired. Substituting the strongest, weakest or default rung would show a
    strength the engine was never set to, and the mismatch would be attributed to
    the engine.

    How a regression manifests: the card names a rung that no profile defines.
    """
    path = tmp_path / "eng.uci"
    path.write_text(
        "[Default]\nUCI_LimitStrength = false\n", encoding="utf-8"
    )
    assert (
        ep.strength_section_display(str(path), "Profile-missing", projection)
        == "Profile-missing"
    )


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


def test_info_fields_cannot_be_written():
    """Submitting an informational option is rejected as read-only.

    Why: UCI_EngineAbout is for the GUI to display; writing it into a profile
    would persist a setoption that engines ignore or misuse. How regression
    shows: validation succeeds and the key appears in the coerced write dict.
    """
    groups = (
        ep.ProfileGroup("about", "About", (
            ep.ProfileField(
                "UCI_EngineAbout", "About", "info",
                "see https://example.com",
            ),
        )),
    )
    err = ep.validation_error(groups, {"UCI_EngineAbout": "changed"})
    assert err is not None
    assert "read-only" in err


def test_delete_blocked_reason_blocks_reserved_sections_and_allows_others():
    """delete_blocked_reason flags both reserved sections; other names are free.

    Why: [DEFAULT] is engine-wide Threads/Hash; [Default] is the seeded strength
    anchor (Unlimited / median net). Deleting either leaves the picker without a
    true default. How regression shows: Default disappears from the Elo list or
    Threads inheritance breaks after a delete.

    Exact ``DEFAULT`` keeps the ConfigParser message; any other casefold of
    ``default`` (including ``Default``) uses the seeded-profile message so the
    API error still contains ``Default`` for the Elo-picker delete path.
    """
    assert ep.delete_blocked_reason("DEFAULT") == "cannot delete the DEFAULT section"
    assert ep.delete_blocked_reason("Default") == "cannot delete the Default profile"
    assert ep.delete_blocked_reason("default") == "cannot delete the Default profile"
    assert ep.delete_blocked_reason("Attacker") is None


def test_delete_matches_existing_section_case_insensitively(uci_file):
    """Deleting 'attacker' removes the on-disk 'Attacker' section.

    Why: URL/path casing should not leave an orphan section. How regression
    shows: delete returns False / Attacker remains.
    """
    assert ep.delete_profile(str(uci_file), "attacker") is True
    assert "Attacker" not in ep.read_profile_names(str(uci_file))


# ---------------------------------------------------------------------------
# Metadata keys: what is ours and what belongs to the engine
# ---------------------------------------------------------------------------


def test_app_metadata_is_never_offered_to_the_engine_as_an_option():
    """A section carries this app's own keys beside real UCI options.

    Why this exists: the engine player sends every key of the chosen section as a
    ``setoption``, and ``[DEFAULT]`` keys are inherited into every section, so a
    metadata key stored there reaches the engine on every game. Many engines
    accept unknown option names silently, so the symptom is not an error: it is
    an engine configured with a value it does not understand. This filter used to
    be copy-pasted at six call sites listing only ``Description``, which is why a
    new metadata key could not be added without an audit.

    How a regression manifests: ``setoption name ProfileLabel`` is sent at game
    start, and an engine that rejects unknown options fails to launch.
    """
    values = {
        "Description": "Aggressive",
        "Name": "Club Player",
        "ProfileLabel": "UCI_Elo",
        "UCI_Elo": "1400",
        "Hash": "16",
    }
    assert ep.uci_options_only(values) == {"UCI_Elo": "1400", "Hash": "16"}


def test_a_user_name_is_accepted_as_metadata_not_refused_as_an_option(groups):
    """``Name`` holds the label a user chose, so it must pass validation.

    Why this exists: validation is deliberately strict -- an unknown key would be
    written verbatim and then offered to the engine as an option it silently
    ignores -- so every key must be either a probed option or a declared piece of
    metadata. Profile identities are generated now, so a user-authored label has
    nowhere else to live.

    How a regression manifests: saving a renamed profile fails with "unknown
    parameter 'Name'", and a profile can no longer be given a name at all.
    """
    coerced = ep.validate_profile_values(
        groups, {"Name": "Club Player", "UCI_Elo": 1500}
    )
    assert coerced == {"Name": "Club Player", "UCI_Elo": "1500"}


@pytest.mark.parametrize(
    "name",
    ["x" * 65, "two\nlines", 42, ["Club"]],
)
def test_a_name_that_cannot_be_shown_is_refused(groups, name):
    # The name is rendered in the picker, the game card and the PGN, so a
    # multi-line or over-long value would break each of those in a different
    # place. A regression accepts it here and fails somewhere further away.
    with pytest.raises(ep.ProfileValidationError):
        ep.validate_profile_values(groups, {"Name": name})


def test_writing_a_profile_keeps_metadata_the_submission_left_out(uci_file, groups):
    """A save that says nothing about the name must not erase it.

    Why this exists: ``write_profile`` replaces the section wholesale, so any key
    the submission omits is gone. The editor submits option values, and the name
    is set elsewhere (and by a different request), so without this the first save
    after naming a profile silently un-names it -- and with labels projected from
    values, the profile then reads as a different profile entirely.

    How a regression manifests: exactly that -- the name disappears from the
    picker after any edit to the profile's options.
    """
    path = str(uci_file)
    ep.write_profile(path, "Attacker", {"Name": "Club Player", "OwnAttack": 120}, groups)
    ep.write_profile(path, "Attacker", {"OwnAttack": 130}, groups)

    values = next(p["values"] for p in ep.read_profiles(path) if p["name"] == "Attacker")
    assert values == {
        "Name": "Club Player",
        "Description": "Aggressive attacking style",
        "OwnAttack": "130",
    }


def test_clearing_a_name_removes_it_rather_than_storing_an_empty_one(uci_file, groups):
    # Renaming back to nothing must leave the profile labelled by its values
    # again. An empty stored Name would render as a blank label -- a picker row
    # with no text -- which is worse than the derived label it replaced.
    path = str(uci_file)
    ep.write_profile(path, "Attacker", {"Name": "Club Player", "OwnAttack": 120}, groups)
    ep.write_profile(path, "Attacker", {"Name": "", "OwnAttack": 120}, groups)

    values = next(p["values"] for p in ep.read_profiles(path) if p["name"] == "Attacker")
    assert values == {"Description": "Aggressive attacking style", "OwnAttack": "120"}


def test_metadata_keys_are_matched_however_they_are_spelled():
    # The file is hand-editable and INI keys are conventionally case-insensitive,
    # so a lower-cased metadata key must not slip through as an option.
    assert ep.uci_options_only({"description": "x", "profilelabel": "UCI_Elo"}) == {}


# ---------------------------------------------------------------------------
# Per-install label key selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("PersonalityFile, UCI_Elo", ("PersonalityFile", "UCI_Elo")),
        ("PersonalityFile,UCI_Elo", ("PersonalityFile", "UCI_Elo")),
        ("  UCI_Elo  ", ("UCI_Elo",)),
        ("UCI_Elo,,", ("UCI_Elo",)),      # a trailing separator is not a key
        ("", ()),
        ("   ", ()),
    ],
)
def test_the_label_key_selection_is_read_from_the_engine_wide_section(
    tmp_path, stored, expected
):
    """A per-install label selection lives in ``[DEFAULT] ProfileLabel``.

    Why this exists: which options identify a profile is a property of the
    install (which engine build, which personality files are present), not of the
    shipped catalog, so it has to be storable beside the profiles it labels.
    ``[DEFAULT]`` is the section every write preserves verbatim, so the selection
    survives editing profiles.

    How a regression manifests: the selection is parsed as one key containing
    commas, matches no probed option, and every profile falls back to the derived
    label with no sign that the selection was read at all.
    """
    path = tmp_path / "engine.uci"
    path.write_text(f"[DEFAULT]\nProfileLabel = {stored}\n\n[Default]\n", encoding="utf-8")
    assert ep.read_label_keys(str(path)) == expected


def test_no_stored_selection_reads_as_no_selection(uci_file):
    # The overwhelmingly common case: the sample config declares no selection, so
    # the caller must fall back to the catalog/derived keys rather than to an
    # empty label.
    assert ep.read_label_keys(str(uci_file)) == ()
    assert ep.read_label_keys(str(uci_file / "missing")) == ()


def test_the_label_selection_survives_a_profile_write(uci_file, groups):
    # The selection is stored in the section write_profile rewrites around, so
    # this pins that editing a profile does not drop it -- losing it would
    # silently revert every label on the next save.
    path = str(uci_file)
    text = uci_file.read_text(encoding="utf-8")
    uci_file.write_text(
        text.replace("[DEFAULT]", "[DEFAULT]\nProfileLabel = UCI_Elo"), encoding="utf-8"
    )
    ep.write_profile(path, "Attacker", {"OwnAttack": 130}, groups)
    assert ep.read_label_keys(path) == ("UCI_Elo",)


# ---------------------------------------------------------------------------
# Resolving a stored reference to a section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("Attacker", "Attacker"),      # the id itself
        ("attacker", "Attacker"),      # legacy: sole case-insensitive section
        ("1200 ELO", "1200 ELO"),      # legacy: a name the seeder once wrote
        ("Default", "Default"),
        ("Missing", None),             # nothing to resolve to, and none invented
        ("", None),
        (None, None),
    ],
)
def test_a_stored_reference_resolves_to_the_section_it_names(
    uci_file, reference, expected
):
    """A player's strength is stored as a section reference and must resolve.

    Why this exists: the reference outlives the config it points into. Sections
    are identified by generated ids now, but a config predating that carries
    names, and configs cross installs through Centaur SD import and backup
    restore. An unresolved reference is not an error the user sees: the engine
    player falls back to the file's engine-wide ``[DEFAULT]`` at game start, so
    the board plays at a strength nobody chose, silently.

    How a regression manifests: a legacy named section stops resolving after the
    switch to ids, and every pre-existing player slot quietly loses its strength.
    """
    assert ep.resolve_section(str(uci_file), reference) == expected


def test_a_reference_resolves_by_user_name_when_no_section_matches(uci_file, groups):
    # A profile can be renamed by its user-authored Name while keeping its id, so
    # a reference written when the name was the identity has to fall back to it.
    # A regression drops the slot to [DEFAULT] the first time a named profile is
    # reached through its old name.
    path = str(uci_file)
    ep.write_profile(path, "Tactical", {"Name": "Club Player", "OwnAttack": 120}, groups)
    assert ep.resolve_section(path, "Club Player") == "Tactical"
    assert ep.resolve_section(path, "club player") == "Tactical"


def test_an_ambiguous_reference_resolves_to_nothing(uci_file):
    # Two sections differing only by case are the legacy state the reconcile UI
    # exists for. Picking either would be a guess at which strength was meant, so
    # the caller must be told it is unresolved (and repoint to Default) instead.
    uci_file.write_text(
        uci_file.read_text(encoding="utf-8") + "\n[attacker]\nOwnAttack = 90\n",
        encoding="utf-8",
    )
    assert ep.resolve_section(str(uci_file), "ATTACKER") is None
    # An exact spelling is never ambiguous, even with a twin present.
    assert ep.resolve_section(str(uci_file), "attacker") == "attacker"


# ---------------------------------------------------------------------------
# The options an engine is actually sent at game start
# ---------------------------------------------------------------------------


def test_the_options_sent_to_the_engine_merge_the_engine_wide_defaults(uci_file):
    """A profile's effective options include ``[DEFAULT]``, minus app metadata.

    Why this exists: this is the one reader the engine-backed players use at game
    start, replacing three copies of the same configparser walk. Hash/Threads live
    only in ``[DEFAULT]`` and must reach the engine, while ``Description`` is this
    app's and is not a UCI option -- an engine that rejects an unknown option name
    fails its handshake, and one that ignores it logs noise on every load.

    How a regression manifests: Hash/Threads silently revert to engine built-ins
    (a performance change with no error), or ``Description`` is sent as setoption.
    """
    options = ep.uci_options_for_section(str(uci_file), "1200 ELO")
    assert options == {
        "Hash": "16",
        "Threads": "2",
        "UCI_LimitStrength": "true",
        "UCI_Elo": "1200",
    }


def test_the_options_for_an_unresolved_reference_are_the_defaults_alone(uci_file):
    """An unresolvable strength yields ``[DEFAULT]`` only, never a guessed rung.

    Why this exists: the selection is already lost by the time the engine loads,
    and picking a nearby rung would put the board at a strength nobody chose while
    looking deliberate. The engine-wide block is still applied so Hash/Threads
    hold.

    How a regression manifests: a fabricated ``UCI_Elo``/``UCI_LimitStrength``
    appears in the result, or the engine-wide options are dropped along with the
    section.
    """
    assert ep.uci_options_for_section(str(uci_file), "Missing") == {
        "Hash": "16",
        "Threads": "2",
    }
    assert ep.uci_options_for_section(str(uci_file), None) == {
        "Hash": "16",
        "Threads": "2",
    }


def test_a_reference_by_legacy_name_still_reaches_its_section(tmp_path):
    # Resolution runs before the read, so a stored name that is now only a user
    # Name (the section itself having a generated id) still loads its options. A
    # regression sends the engine the defaults instead, at full strength.
    path = tmp_path / "renamed.uci"
    path.write_text(
        "[DEFAULT]\nHash = 16\n\n"
        "[Profile-a1b2c3]\nName = Attacker\nOwnAttack = 125\n",
        encoding="utf-8",
    )

    options = ep.uci_options_for_section(str(path), "Attacker")
    assert options["OwnAttack"] == "125"
    assert "Name" not in options


def test_the_options_for_a_missing_file_are_empty(tmp_path):
    # A custom engine can have no .uci file at all; the caller must get an empty
    # mapping rather than an exception on a path that does not exist.
    assert ep.uci_options_for_section(str(tmp_path / "absent.uci"), "Default") == {}


# ---------------------------------------------------------------------------
# Reading a section's playing strength
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("1200 ELO", 1200),   # capped rung: the Elo it actually plays
        ("1200 elo", 1200),   # sole case-insensitive match resolves like the writers
        ("Default", None),    # UCI_LimitStrength=false: no single Elo to report
        ("Attacker", None),   # sets no Elo at all
        ("Missing", None),    # absent section
        ("", None),
    ],
)
def test_strength_section_elo_reads_the_elo_out_of_the_section(
    uci_file, section, expected
):
    """The Elo comes from the section's values, never from its identity.

    Why this exists: Auto coach selection needs a number for the opponent, and it
    used to scrape the first run of digits out of the stored selection because the
    seeded ladder spelled the Elo into the section name. Identities are generated
    now, so that scrape returns the identity's digits -- "Profile-1" read as a
    1-rated opponent, and the coach sized itself against it with no error.

    How a regression manifests: an uncapped or Elo-less section returns a number
    (a fabricated rating), or a capped rung returns None and every engine opponent
    reads as unknown.
    """
    assert ep.strength_section_elo(str(uci_file), section) == expected


# ---------------------------------------------------------------------------
# Profile name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "1200 ELO",              # seeded rung
    "Club Player",           # internal space
    "Tal",
    "Semi-Slav",             # hyphen
    "Defender: 1700 ELO",    # colon, for a composed label
    "Angriff (scharf)",      # parentheses
    "Verteidiger_2",         # underscore
    "Zugzwang, spät",        # comma and a non-ASCII letter
])
def test_valid_profile_names_accepted(name):
    """Names real profiles use are accepted by the allowlist.

    Why: the allowlist has to admit what users actually type, in five UI
    languages. How the regression shows: a legitimate name is rejected with
    "Invalid profile name" and cannot be created at all -- a stricter failure
    than the blocklist it replaced, so the accepted set is pinned explicitly.
    """
    assert ep.is_valid_profile_name(name) is True


@pytest.mark.parametrize("name", [
    "",            # empty
    "  ",          # whitespace only
    " Tal",        # leading space (would be trimmed by configparser -> mismatch)
    "DEFAULT",     # reserved engine-wide defaults section
    "Default",     # reserved seeded strength profile (edit = save-as new name)
    "default",     # case-insensitive reserved
    "DeFaUlT",     # case-insensitive reserved
    "a[b]",        # brackets break the INI header
    "x\ny",        # newline
    "x" * 65,      # too long
    "a/b",         # slash: valid INI, unroutable REST path segment
    "a\\b",        # backslash: path separator on the import/restore side
    "50%",         # would become an interpolation token if interpolation returned
    "a;b",         # INI comment delimiter
    "a#b",         # INI comment delimiter
    "a\tb",        # tab, invisible in the picker
])
def test_invalid_profile_names_rejected(name):
    """Names outside the allowlist or colliding with reserved sections are rejected.

    Why Default is reserved: overwriting it would leave a section still named
    Default whose options are no longer the seeded default (Maia net / uncapped
    Stockfish). Edits must be saved under a new name instead. Case variants are
    rejected too because ConfigParser keeps case-distinct sections.

    Why the rest: the name is also a REST path segment and an INI header, and the
    former four-character blocklist only covered what breaks the INI. A slash is
    the case that mattered -- it writes a perfectly good section that Flask's
    default string converter will not match, so the profile could be created and
    then never saved or deleted through its own endpoints. How the regression
    shows: a name here is accepted, and for the slash the editor then offers Save
    and Delete buttons that 404 forever.
    """
    assert ep.is_valid_profile_name(name) is False


@pytest.mark.parametrize("name", ["Default", "default", "DEFAULT", "DeFaUlT"])
def test_is_reserved_profile_name_case_insensitive(name):
    """Reserved Default/DEFAULT collide regardless of spelling case.

    Why: a twin [default] section would look like Default in the picker while
    bypassing seed ownership. How regression shows: is_reserved false for
    "default" and write_profile creates a second section.
    """
    assert ep.is_reserved_profile_name(name) is True
    assert ep.is_reserved_profile_name("1200 ELO") is False


def test_matching_section_name_is_case_insensitive():
    """Find the on-disk spelling for a typed name that differs only by case."""
    names = ["Default", "1200 ELO", "Attacker"]
    assert ep.matching_section_name(names, "1200 elo") == "1200 ELO"
    assert ep.matching_section_name(names, "attacker") == "Attacker"
    assert ep.matching_section_name(names, "Fresh") is None


def test_matching_section_name_prefers_exact_when_case_twins_exist():
    """Exact spelling wins when both Attacker and attacker are on disk.

    Why: remapping to the first casefold match silently overwrote the other
    twin. How regression shows: matching_section_name('attacker') returns
    'Attacker' when both exist.
    """
    names = ["Attacker", "attacker"]
    assert ep.matching_section_name(names, "attacker") == "attacker"
    assert ep.matching_section_name(names, "Attacker") == "Attacker"
    assert ep.matching_section_name(names, "ATTACKER") is None  # ambiguous


def test_case_collision_groups_lists_twins():
    """case_collision_groups returns only groups with two or more spellings."""
    assert ep.case_collision_groups(["Default", "Attacker", "1200 ELO"]) == []
    assert ep.case_collision_groups(["Attacker", "1200 ELO", "attacker"]) == [
        ["Attacker", "attacker"],
    ]


def test_reconcile_case_duplicate_keeps_chosen_spelling(uci_file, groups):
    """Reconcile keeps one twin's values and removes the other section.

    Why: operators need a safe way to heal legacy case-duplicate .uci files.
    How regression shows: both sections remain, or the kept section's values
    are replaced by the discarded twin's.
    """
    # Bypass write_profile (which remaps sole casefold matches) to create a
    # legacy twin beside Attacker (OwnAttack=125 in SAMPLE_UCI).
    with open(uci_file, "a", encoding="utf-8") as handle:
        handle.write("\n[attacker]\nOwnAttack = 50\n")
    names = ep.read_profile_names(str(uci_file))
    assert "Attacker" in names and "attacker" in names

    removed = ep.reconcile_case_duplicate(str(uci_file), "Attacker")
    assert removed == ["attacker"]
    names = ep.read_profile_names(str(uci_file))
    assert "Attacker" in names
    assert "attacker" not in names
    profiles = {p["name"]: p["values"] for p in ep.read_profiles(str(uci_file))}
    assert profiles["Attacker"]["OwnAttack"] == "125"


def test_write_refuses_ambiguous_case_when_no_exact_match(uci_file, groups):
    """Writing a third casing when twins already exist is rejected.

    Why: without an exact match, remapping would pick an arbitrary twin.
    How regression shows: write succeeds and one twin's values change.
    """
    with open(uci_file, "a", encoding="utf-8") as handle:
        handle.write("\n[attacker]\nOwnAttack = 50\n")
    before = {
        p["name"]: p["values"]
        for p in ep.read_profiles(str(uci_file))
        if p["name"].casefold() == "attacker"
    }
    with pytest.raises(ep.ProfileValidationError, match="ambiguous"):
        ep.write_profile(str(uci_file), "ATTACKER", {"OwnAttack": 99}, groups)
    after = {
        p["name"]: p["values"]
        for p in ep.read_profiles(str(uci_file))
        if p["name"].casefold() == "attacker"
    }
    assert after == before


def test_write_exact_twin_does_not_clobber_other_casing(uci_file, groups):
    """Saving [attacker] leaves [Attacker] untouched when both exist.

    Why: the silent overwrite the user hit. How regression shows: Attacker
    OwnAttack changes when writing attacker.
    """
    with open(uci_file, "a", encoding="utf-8") as handle:
        handle.write("\n[attacker]\nOwnAttack = 50\n")
    before_upper = next(
        p for p in ep.read_profiles(str(uci_file)) if p["name"] == "Attacker"
    )
    ep.write_profile(str(uci_file), "attacker", {"OwnAttack": 77}, groups)
    after_upper = next(
        p for p in ep.read_profiles(str(uci_file)) if p["name"] == "Attacker"
    )
    after_lower = next(
        p for p in ep.read_profiles(str(uci_file)) if p["name"] == "attacker"
    )
    assert after_upper == before_upper
    assert after_lower["values"]["OwnAttack"] == "77"


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


def test_write_replaces_the_options_wholesale(uci_file, groups):
    """Editing a profile replaces its options entirely (removed keys disappear).

    The editor submits the complete desired set of options; a regression that
    merged into the old section would leave stale keys (here 'Style') that the
    user removed. Metadata is the exception and is carried over -- see
    ``test_writing_a_profile_keeps_metadata_the_submission_left_out``.
    """
    ep.write_profile(str(uci_file), "Attacker", {
        "OwnAttack": 200,
    }, groups)
    profiles = {p["name"]: p["values"] for p in ep.read_profiles(str(uci_file))}
    assert profiles["Attacker"] == {
        "OwnAttack": "200",
        "Description": "Aggressive attacking style",
    }


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


def test_write_profile_refuses_to_overwrite_default(uci_file, groups):
    """The seeded Default profile cannot be replaced via write_profile.

    Why: Default is the seed/reconcile-owned strength anchor. Saving edited Maia
    weights (or Stockfish limit-strength) under the name Default would leave a
    section that claims to be default but is not. How regression shows: Default's
    values change after a save while the picker still offers "Default"/"Unlimited".
    """
    before = next(p for p in ep.read_profiles(str(uci_file)) if p["name"] == "Default")
    with pytest.raises(ep.ProfileValidationError):
        ep.write_profile(
            str(uci_file),
            "Default",
            {"UCI_LimitStrength": True, "UCI_Elo": 1500},
            groups,
        )
    after = next(p for p in ep.read_profiles(str(uci_file)) if p["name"] == "Default")
    assert after == before


@pytest.mark.parametrize("name", ["default", "DEFAULT", "DeFaUlT"])
def test_write_profile_refuses_case_variants_of_default(uci_file, groups, name):
    """Case-only variants of Default are rejected the same as Default.

    Why: ConfigParser would otherwise create a twin [default] section. How
    regression shows: write succeeds and read_profile_names includes both
    Default and the typed casing.
    """
    with pytest.raises(ep.ProfileValidationError):
        ep.write_profile(str(uci_file), name, {"OwnAttack": 100}, groups)
    names = ep.read_profile_names(str(uci_file))
    assert names.count("Default") == 1
    assert name not in names


def test_write_updates_existing_section_when_casing_differs(uci_file, groups):
    """Save-as with different casing updates the existing section spelling.

    Why: "attacker" must not create a second section beside "Attacker". How
    regression shows: both Attacker and attacker appear in read_profile_names,
    or Attacker keeps its old OwnAttack value.
    """
    ep.write_profile(str(uci_file), "attacker", {"OwnAttack": 200}, groups)
    names = ep.read_profile_names(str(uci_file))
    assert "Attacker" in names
    assert "attacker" not in names
    profiles = {p["name"]: p["values"] for p in ep.read_profiles(str(uci_file))}
    assert profiles["Attacker"] == {
        "OwnAttack": "200",
        # Options are replaced wholesale; metadata the submission omits is kept.
        "Description": "Aggressive attacking style",
    }


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


def test_delete_seeded_default_profile_refused(uci_file):
    """Refuse to delete the seeded [Default] strength profile.

    Why: the Elo picker and player configs rely on a Default section always
    existing. How regression shows: Default vanishes from /levels after delete.
    """
    with pytest.raises(ep.ProfileValidationError):
        ep.delete_profile(str(uci_file), "Default")
    assert "Default" in ep.read_profile_names(str(uci_file))

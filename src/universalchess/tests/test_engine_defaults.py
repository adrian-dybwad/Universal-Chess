"""App-wide engine defaults (Hash/Threads/Syzygy probe) and per-engine overlay.

Why these tests exist
---------------------
HIARCS-style defaults are one set for the device, inherited by every engine
until that engine unchecks Use shared defaults for a group. Hash/Threads used
to live only in each ``.uci`` ``[DEFAULT]``, so raising them on the Engines
tab could not change Stockfish and Arasan together, and a leftover
``Threads = 1`` from seeding pinned every engine at one core.

How a regression manifests
--------------------------
- Shared Hash is not applied, so play uses the engine's built-in hash.
- ``Move Overhead`` is sent to an engine that never advertised it.
- Use shared off still ignores the engine's Hash, or overlays Syzygy knobs
  when only resources were opted out.
- Seeded ``Threads = 1`` keeps winning after the shared Threads value changes.
- ``UseSharedResources`` is forwarded as a UCI option.
"""

from pathlib import Path

import pytest

from universalchess.services import engine_defaults as ed


@pytest.fixture
def shared_values(monkeypatch):
    """Pin the app-wide set so merge tests do not read centaur.ini."""
    values = {
        "Hash": "16",
        "Threads": "4",
        "Move Overhead": "100",
        "SyzygyProbeLimit": "5",
        "SyzygyProbeDepth": "1",
        "Syzygy50MoveRule": "true",
    }
    monkeypatch.setattr(ed, "values", lambda: dict(values))
    return values


def _write_uci(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_shipped_hash_and_threads_match_the_low_power_seed():
    """The shared floor is the same 16 MB / 1 thread the seed used to write.

    Why: a Pi Zero cannot host a large hash, and the previous seed was Threads=1
    for that reason. Jumping the shipped default to a Pi 5-sized hash would
    thrash every existing small board on upgrade. How a regression manifests:
    DEFAULT_HASH_MB or DEFAULT_THREADS leave 16 / 1.
    """
    assert ed.DEFAULT_HASH_MB == 16
    assert ed.DEFAULT_THREADS == 1


def test_hash_max_is_sixteen_on_a_constrained_board():
    """The Hash slider must not offer more RAM than a 512 MB board can spare.

    Why: Stockfish advertises a multi-terabyte max; using that as the slider
    ceiling lets a tap set Hash to a value that OOMs the board. How a
    regression manifests: hash_max_mb(415) is greater than 16.
    """
    assert ed.hash_max_mb(415) == 16
    assert ed.hash_max_mb(2047) == 16


def test_hash_max_scales_on_a_pi5():
    """A Pi 5 / CM5 with several GB may raise Hash above the 16 MB floor.

    Why: the feature exists so a larger board can use more RAM without each
    engine storing its own Hash. How a regression manifests: hash_max_mb(8192)
    stays 16, or exceeds 1/8 of reported RAM.
    """
    assert ed.hash_max_mb(8192) == 1024
    assert ed.hash_max_mb(4096) == 512
    assert ed.hash_max_mb(None) >= 16


def test_merge_applies_shared_hash_and_threads(shared_values):
    """Play options pick up the app-wide Hash/Threads when Use shared is on.

    Why: this is the whole feature -- one slider, every engine. How a
    regression manifests: merged options omit Hash or keep only the profile
    Elo keys.
    """
    merged = ed.merge_options(
        {"UCI_Elo": "1400"},
        advertised=["Hash", "Threads", "UCI_Elo"],
    )
    assert merged["Hash"] == "16"
    assert merged["Threads"] == "4"
    assert merged["UCI_Elo"] == "1400"


def test_merge_drops_unadvertised_move_overhead(shared_values):
    """Move Overhead is device lag, but only engines that advertise it get it.

    Why: python-chess raises EngineError for unknown option names (and some
    engines exit). Reckless does not advertise Move Overhead. How a regression
    manifests: merged options include Move Overhead when it is not advertised.
    """
    merged = ed.merge_options(
        {},
        advertised=["Hash", "Threads"],
    )
    assert "Move Overhead" not in merged
    assert "Hash" in merged

    with_overhead = ed.merge_options(
        {},
        advertised=["Hash", "Threads", "Move Overhead"],
    )
    assert with_overhead["Move Overhead"] == "100"


def test_leftover_seed_threads_are_ignored_when_using_shared(tmp_path, shared_values):
    """A seeded Threads=1 must not pin the engine after shared Threads changes.

    Why: every existing .uci was seeded with Threads=1. Treating that as an
    override would make the shared slider a no-op. How a regression manifests:
    merged Threads stays 1 while shared values say 4.
    """
    uci = _write_uci(
        tmp_path / "stockfish.uci",
        "[DEFAULT]\nThreads = 1\n\n[Default]\nUCI_LimitStrength = false\n",
    )
    merged = ed.merge_options(
        {"UCI_LimitStrength": "false"},
        advertised=["Threads", "Hash", "UCI_LimitStrength"],
        uci_path=uci,
    )
    assert merged["Threads"] == "4"
    assert merged["Hash"] == "16"


def test_use_shared_off_overlays_only_that_group(tmp_path, shared_values):
    """Unchecking resources replaces Hash/Threads, not Syzygy probe knobs.

    Why: HIARCS Use Defaults is per group. Opting Stockfish out of the RAM
    budget must not also fork its tablebase probe limit. How a regression
    manifests: Hash stays 16, or SyzygyProbeLimit becomes 7 from the file
    while UseSharedSyzygy is still on.
    """
    uci = _write_uci(
        tmp_path / "stockfish.uci",
        "[DEFAULT]\n"
        "UseSharedResources = false\n"
        "Hash = 64\n"
        "Threads = 3\n"
        "SyzygyProbeLimit = 7\n"
        "\n[Default]\nUCI_Elo = 1400\n",
    )
    merged = ed.merge_options(
        {"UCI_Elo": "1400"},
        advertised=["Hash", "Threads", "SyzygyProbeLimit", "UCI_Elo"],
        uci_path=uci,
    )
    assert merged["Hash"] == "64"
    assert merged["Threads"] == "3"
    assert merged["SyzygyProbeLimit"] == "5"
    assert merged["UCI_Elo"] == "1400"


def test_use_shared_syzygy_off_overlays_probe_knobs(tmp_path, shared_values):
    """Unchecking the Syzygy group uses the engine's own probe limit.

    Why: the matching overlay for tablebase policy. How a regression
    manifests: SyzygyProbeLimit stays 5 while the file says 7.
    """
    uci = _write_uci(
        tmp_path / "arasan.uci",
        "[DEFAULT]\n"
        "UseSharedSyzygy = false\n"
        "SyzygyProbeLimit = 7\n"
        "SyzygyProbeDepth = 4\n"
        "Syzygy50MoveRule = false\n"
        "\n[Default]\nUCI_LimitStrength = false\n",
    )
    merged = ed.merge_options(
        {},
        advertised=["SyzygyProbeLimit", "SyzygyProbeDepth", "Syzygy50MoveRule", "Hash"],
        uci_path=uci,
    )
    assert merged["SyzygyProbeLimit"] == "7"
    assert merged["SyzygyProbeDepth"] == "4"
    assert merged["Syzygy50MoveRule"] == "false"
    assert merged["Hash"] == "16"


def test_metadata_keys_are_never_sent_as_options(tmp_path, shared_values):
    """UseShared* flags belong to the app, not to the engine.

    Why: an engine that rejects unknown option names fails its handshake;
    one that ignores them logs noise on every load. How a regression
    manifests: UseSharedResources or UseSharedSyzygy appears in merged options.
    """
    uci = _write_uci(
        tmp_path / "eng.uci",
        "[DEFAULT]\nUseSharedResources = true\nUseSharedSyzygy = true\n",
    )
    merged = ed.merge_options(
        {"UseSharedResources": "true"},
        advertised=["Hash", "Threads", "UseSharedResources"],
        uci_path=uci,
    )
    assert "UseSharedResources" not in merged
    assert "UseSharedSyzygy" not in merged
    assert merged["Hash"] == "16"


def test_set_use_shared_off_copies_current_shared_values(tmp_path, shared_values):
    """Unchecking Use shared snapshots the shared set into the engine file.

    Why: the local editors must open on what the engine was already playing,
    not blank or the engine's built-in defaults. How a regression manifests:
    UseSharedResources is false but Hash is missing from [DEFAULT].
    """
    uci = _write_uci(tmp_path / "eng.uci", "[DEFAULT]\n\n[Default]\nUCI_LimitStrength = false\n")
    ed.set_use_shared(uci, ed.GROUP_RESOURCES, False)
    text = Path(uci).read_text(encoding="utf-8")
    assert "UseSharedResources = false" in text
    assert "Hash = 16" in text
    assert "Threads = 4" in text


def test_status_includes_hash_max_and_values(monkeypatch):
    """The Engines card needs the current values and the RAM-aware Hash ceiling.

    Why: the slider max must come from the server (device RAM), not from
    Stockfish's advertised spin max. How a regression manifests: status omits
    hash_max_mb or hash.
    """
    monkeypatch.setattr(ed, "_ram_mb", lambda: 8192)
    snapshot = ed.status()
    assert snapshot["hash"] == ed.DEFAULT_HASH_MB
    assert snapshot["threads"] == ed.DEFAULT_THREADS
    assert snapshot["move_overhead"] == ed.DEFAULT_MOVE_OVERHEAD_MS
    assert snapshot["syzygy_probe_limit"] == ed.DEFAULT_SYZYGY_PROBE_LIMIT
    assert snapshot["hash_max_mb"] == 1024
    assert snapshot["threads_max"] == ed.THREADS_SLIDER_MAX
    assert snapshot["move_overhead_max"] == ed.MOVE_OVERHEAD_SLIDER_MAX_MS
    assert snapshot["constrained"] is False


def test_schema_json_caps_resource_sliders_to_the_device(monkeypatch):
    """Per-engine Hash/Threads tracks must match Shared engine defaults.

    Why: Stockfish advertises Hash 2048 / Threads 1024, so the overlay
    slider sat on a different range than the shared card. How a
    regression manifests: schema JSON still reports Hash max 2048.
    """
    from universalchess.services.engine_profiles import ProfileField, ProfileGroup, schema_to_json

    monkeypatch.setattr(ed, "hash_max_mb", lambda ram_mb=None: 16)
    groups = (
        ProfileGroup(
            "resources",
            "Resources",
            (
                ProfileField("Hash", "Hash", "int", 16, minimum=1, maximum=2048),
                ProfileField("Threads", "Threads", "int", 1, minimum=1, maximum=1024),
                ProfileField(
                    "Move Overhead", "Move Overhead", "int", 100, minimum=0, maximum=5000
                ),
                ProfileField("UCI_Elo", "ELO", "int", 1500, minimum=800, maximum=2800),
            ),
        ),
    )
    by_key = {field["key"]: field for group in schema_to_json(groups) for field in group["fields"]}
    assert by_key["Hash"]["max"] == 16
    assert by_key["Threads"]["max"] == ed.THREADS_SLIDER_MAX
    assert by_key["Move Overhead"]["max"] == ed.MOVE_OVERHEAD_SLIDER_MAX_MS
    assert by_key["UCI_Elo"]["max"] == 2800


def test_set_values_rejects_threads_above_the_slider_cap(monkeypatch):
    """A crafted POST must not store more threads than the shared slider allows.

    Why: set_values used to clamp Threads at 256 while the card tops out at 8.
    How a regression manifests: threads 64 is stored as 64.
    """
    stored = {}

    def fake_save(section, key, value, **kwargs):
        if section == ed.SETTING_SECTION:
            stored[key] = value
        return True

    monkeypatch.setattr(ed, "save_setting", fake_save)
    assert ed.set_values({"threads": 64, "move_overhead": 5000}) is True
    assert stored["threads"] == ed.THREADS_SLIDER_MAX
    assert stored["move_overhead"] == ed.MOVE_OVERHEAD_SLIDER_MAX_MS


def test_engine_overlay_hash_is_clamped_to_the_device(tmp_path, monkeypatch):
    """Unchecking Use shared must not write Stockfish's advertised Hash max.

    Why: the overlay POST used to store whatever the engine advertised.
    How a regression manifests: [DEFAULT] Hash = 2048 on a 16 MB board.
    """
    monkeypatch.setattr(ed, "hash_max_mb", lambda ram_mb=None: 16)
    uci = _write_uci(tmp_path / "eng.uci", "[DEFAULT]\nUseSharedResources = false\n")
    ed.set_engine_group_values(uci, ed.GROUP_RESOURCES, {"Hash": 2048, "Threads": 64})
    text = Path(uci).read_text(encoding="utf-8")
    assert "Hash = 16" in text
    assert "Threads = 8" in text

"""Tests for the optional 3–5-piece Syzygy tablebase service.

Why these tests exist
---------------------
Tablebases are not shipped, and probing them on a 512 MiB board fights NNUE
and the hash table. The service must therefore default off, require a complete
file set before any engine is pointed at the folder, share one directory, and
refuse a download that cannot fit. A half-installed folder silently mixing
tablebase hits with search would be worse than no tables at all.

How a regression manifests
--------------------------
- ``table_names`` drifting from the 145 Lichess files 404s the download.
- ``should_probe`` true with a partial folder points Stockfish at missing
  material.
- ``merge_path`` overwriting a custom SyzygyPath, or injecting into an engine
  that does not advertise the option (Reckless/Zahak).
- Download proceeding when free space is below the 3–5-piece footprint.
"""

from pathlib import Path

import pytest

from universalchess.services import syzygy


@pytest.fixture(autouse=True)
def isolate_download_state():
    syzygy.reset_download_state_for_tests()
    yield
    syzygy.reset_download_state_for_tests()


def test_table_names_are_the_lichess_3_4_5_set():
    """The generator must emit the 145 canonical 3–5-piece names Lichess hosts.

    Why: the download URL is ``.../3-4-5-wdl/{name}.rtbw``. A renamed or extra
    stem 404s; a missing stem leaves ``is_ready`` false forever. How a
    regression manifests: the count leaves 145, or KQvK / KPvKP / KBBNvK (the
    3-, 4-, and 5-piece shapes) drop out of the set.
    """
    names = syzygy.table_names()
    assert len(names) == syzygy.EXPECTED_TABLES
    assert len(names) == len(set(names))
    three = [name for name in names if len(name.replace("v", "")) == 3]
    four = [name for name in names if len(name.replace("v", "")) == 4]
    five = [name for name in names if len(name.replace("v", "")) == 5]
    assert three == ["KBvK", "KNvK", "KPvK", "KQvK", "KRvK"]
    assert len(four) == 30
    assert len(five) == 110
    assert "KPvKP" in names
    assert "KBBNvK" in names
    assert "KQPvKN" in names


def test_file_jobs_are_https_wdl_and_dtz_pairs():
    """Each table is fetched as WDL then DTZ from the HTTPS Lichess trees.

    Why: the combined ``/3-4-5/`` tree 404s; WDL and DTZ now live in sibling
    directories. HTTP would also be rejected by the fetch guard. How a
    regression manifests: jobs point at ``/3-4-5/`` or ``http://``, and the
    download fails on the first file.
    """
    jobs = syzygy.file_jobs()
    assert len(jobs) == syzygy.EXPECTED_FILES
    urls = [url for url, _ in jobs]
    assert all(url.startswith("https://") for url in urls)
    assert any("/3-4-5-wdl/KQvK.rtbw" in url for url in urls)
    assert any("/3-4-5-dtz/KQvK.rtbz" in url for url in urls)
    assert not any("/3-4-5/KQvK." in url for url in urls)


def _write_pair(folder: Path, name: str) -> None:
    (folder / f"{name}.rtbw").write_bytes(b"WDL")
    (folder / f"{name}.rtbz").write_bytes(b"DTZ")


def test_partial_folder_is_not_ready(tmp_path):
    """One complete pair is not enough to probe.

    Why: Stockfish given a SyzygyPath with only some files probes those
    endings and searches the rest, so play is a silent mix of oracle and
    heuristic. How a regression manifests: ``is_ready`` / ``should_probe``
    become true as soon as any ``.rtbw`` exists.
    """
    _write_pair(tmp_path, "KQvK")
    assert syzygy.present_stems(str(tmp_path)) == ("KQvK",)
    assert syzygy.is_ready(str(tmp_path)) is False


def test_empty_files_do_not_count(tmp_path):
    """A zero-byte file is a failed download, not an installed table.

    Why: a stopped fetch can leave an empty dest; treating it as present would
    mark the set ready and skip re-download. How a regression manifests:
    ``present_stems`` includes a stem whose files have size 0.
    """
    (tmp_path / "KQvK.rtbw").write_bytes(b"")
    (tmp_path / "KQvK.rtbz").write_bytes(b"DTZ")
    assert syzygy.present_stems(str(tmp_path)) == ()


def test_complete_folder_is_ready(tmp_path):
    """All 145 pairs present is the only ready state.

    Why: ``should_probe`` keys off this. How a regression manifests: ready is
    true with 144 pairs, or false with 145.
    """
    for name in syzygy.table_names():
        _write_pair(tmp_path, name)
    assert syzygy.is_ready(str(tmp_path)) is True
    assert len(syzygy.present_stems(str(tmp_path))) == syzygy.EXPECTED_TABLES


def test_should_probe_requires_enabled_and_ready(tmp_path, monkeypatch):
    """Probing is off unless the user turned it on *and* the set is complete.

    Why: files on disk without the toggle would surprise a low-RAM board that
    copied them in; the toggle without files would send an empty SyzygyPath.
    How a regression manifests: either flag alone makes ``should_probe`` true.
    """
    monkeypatch.setattr(syzygy, "is_enabled", lambda: False)
    for name in syzygy.table_names():
        _write_pair(tmp_path, name)
    assert syzygy.should_probe(str(tmp_path)) is False

    monkeypatch.setattr(syzygy, "is_enabled", lambda: True)
    assert syzygy.should_probe(str(tmp_path)) is True
    assert syzygy.should_probe(str(tmp_path / "empty")) is False


def test_merge_path_injects_only_when_advertised_and_ready():
    """SyzygyPath is added only for engines that advertise it, and only then.

    Why: Reckless/Zahak are built without probing; python-chess would ignore
    the option, but the profile editor would still show a path that does
    nothing. A custom path must win so Advanced still works. How a regression
    manifests: Reckless receives SyzygyPath, or a user path is overwritten.
    """
    folder = "/opt/universalchess/syzygy"
    injected = syzygy.merge_path(
        {"Threads": "1"},
        ["Threads", "SyzygyPath"],
        enabled=True,
        ready=True,
        directory=folder,
    )
    assert injected == {"Threads": "1", "SyzygyPath": folder}

    skipped = syzygy.merge_path(
        {"Threads": "1"},
        ["Threads"],
        enabled=True,
        ready=True,
        directory=folder,
    )
    assert skipped == {"Threads": "1"}

    custom = syzygy.merge_path(
        {"SyzygyPath": "/mnt/ssd/tb"},
        ["SyzygyPath"],
        enabled=True,
        ready=True,
        directory=folder,
    )
    assert custom == {"SyzygyPath": "/mnt/ssd/tb"}

    disabled = syzygy.merge_path(
        {"Threads": "1"},
        ["SyzygyPath"],
        enabled=False,
        ready=True,
        directory=folder,
    )
    assert disabled == {"Threads": "1"}


def test_status_flags_constrained_below_two_gigabytes(tmp_path, monkeypatch):
    """``constrained`` follows RAM, not disk, and is off when RAM is unknown.

    Why: the UI uses this flag for the extra low-power warning. Treating a
    missing ``/proc/meminfo`` (macOS) as constrained would scare a desktop
    install; ignoring a 415 MiB reading would hide the warning on dgt-64.
    How a regression manifests: ``constrained`` is True on None, or False at
    415.
    """
    monkeypatch.setattr(syzygy, "is_enabled", lambda: False)
    monkeypatch.setattr(syzygy, "_mem_total_mb", lambda: 415)
    snapshot = syzygy.status(str(tmp_path))
    assert snapshot["constrained"] is True
    assert snapshot["ram_mb"] == 415
    assert snapshot["ready"] is False
    assert snapshot["enabled"] is False
    assert snapshot["expected"] == 145
    assert snapshot["download_mib"] == 939

    monkeypatch.setattr(syzygy, "_mem_total_mb", lambda: None)
    assert syzygy.status(str(tmp_path))["constrained"] is False

    monkeypatch.setattr(syzygy, "_mem_total_mb", lambda: 8192)
    assert syzygy.status(str(tmp_path))["constrained"] is False


def test_download_skips_existing_and_writes_missing(tmp_path):
    """Resume must not re-fetch files that are already present.

    Why: a 290-file download that is stopped and restarted would otherwise
    redo ~1 GB. How a regression manifests: the fetch is called for KQvK
    even though both files are already non-empty, or a missing DTZ is never
    requested.
    """
    _write_pair(tmp_path, "KQvK")
    fetched = []

    def fetch(url, dest):
        fetched.append((url, dest.name))
        dest.write_bytes(b"data")

    # Stop after the first missing file so this does not write 290 files.
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 4

    syzygy.download_tables(
        directory=str(tmp_path),
        fetch=fetch,
        should_stop=should_stop,
    )
    assert not any(name.startswith("KQvK.") for _, name in fetched)
    assert fetched  # at least one missing file was requested


def test_download_refuses_non_https_url(tmp_path):
    """The default fetch must not follow a non-https URL.

    Why: the same S310 concern as verify_endgames.py -- a swapped base URL
    must not open file: or http:. How a regression manifests: an http URL is
    fetched instead of raising.
    """
    with pytest.raises(ValueError, match="https"):
        syzygy._default_fetch("http://tablebase.lichess.ovh/tables/standard/3-4-5-wdl/KQvK.rtbw", tmp_path / "KQvK.rtbw")


def test_start_download_refuses_when_disk_is_short(tmp_path, monkeypatch):
    """A download that cannot fit must not start.

    Why: filling the SD card takes the board down (logs, game DB, swap). How
    a regression manifests: ``start_download`` returns accepted True when free
    space is below ``MIN_FREE_BYTES``.
    """
    monkeypatch.setattr(syzygy, "_free_bytes", lambda _directory: 10 * 1024 * 1024)
    accepted, message = syzygy.start_download(str(tmp_path))
    assert accepted is False
    assert "disk" in message.lower()
    assert syzygy.status(str(tmp_path))["downloading"] is False


def test_delete_removes_only_expected_stems(tmp_path):
    """Delete must not sweep unrelated files in the shared folder.

    Why: a user may keep notes or a 6-piece file they copied themselves. How
    a regression manifests: ``notes.txt`` disappears, or KQvK.rtbw remains.
    """
    _write_pair(tmp_path, "KQvK")
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")
    removed = syzygy.delete_tables(str(tmp_path))
    assert removed == 2
    assert (tmp_path / "notes.txt").is_file()
    assert not (tmp_path / "KQvK.rtbw").exists()
    assert syzygy.present_stems(str(tmp_path)) == ()

"""Tests for the SD-capture helper (tools/centaur-import/make-centaur-image.sh).

The helper reads the original DGT Centaur SD card's ext4 root partition into a
gzip image for upload (System -> Original Centaur -> Import from SD). Its Linux
backend has to pick the right partition out of the card's block devices: the
ext4 root (largest ext partition) and never the vfat boot partition or the whole
disk, since dd'ing either produces an image the importer cannot loop-mount.

That selection only runs against real block devices, so the tests drive the
script end-to-end with the external commands it shells out to (uname, lsblk,
sudo, dd) stubbed on PATH -- the boundary between the script and the OS. The
stub block-device table is shared with the assertions so expected sizes and
device names cannot drift.
"""

import gzip
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "centaur-import" / "make-centaur-image.sh"

# Block devices the stub `lsblk` reports for the fake card, mirroring a real
# Centaur SD: a vfat boot partition, the ext4 root holding the app, a smaller
# ext4 data partition, and an unformatted partition (empty FSTYPE column, which
# shifts lsblk's whitespace-separated columns and must not be misparsed).
_BLOCK_DEVICES = (
    # name, size bytes, fstype, type
    ("sdb", 3965190144, "", "disk"),
    ("sdb1", 46137344, "vfat", "part"),
    ("sdb2", 209715200, "ext4", "part"),
    ("sdb3", 20971520, "ext4", "part"),
    ("sdb4", 1048576, "", "part"),
)
_CARD_DISK = "sdb"
_ROOT_PART = "sdb2"      # largest ext -> the app root
_DATA_PART = "sdb3"      # smaller ext -> persistent data
_BOOT_PART = "sdb1"      # vfat -> never imaged
_UNFORMATTED_PART = "sdb4"

# A second card whose partitions lsblk cannot identify: real lsblk leaves FSTYPE
# empty when neither udev data nor device read access is available, so the ext4
# partitions are indistinguishable from unformatted ones.
_UNPROBED_DISK = "sdc"
_UNPROBED_DEVICES = tuple(
    (name.replace(_CARD_DISK, _UNPROBED_DISK), size, "", devtype)
    for name, size, _fs, devtype in _BLOCK_DEVICES
)


def _size_of(name):
    return next(size for n, size, _fs, _t in _BLOCK_DEVICES if n == name)


def _stub_payload(device):
    """Bytes the stub `dd` emits for a device, so each image is identifiable."""
    return f"payload-for-{device}\n".encode()


def _write_stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _device_table_case(disk, devices):
    """One `case` branch of the stub: every query shape answered for `disk`.

    The row layout mirrors real `lsblk --list --paths` output (flat rows, /dev
    paths, an empty FSTYPE column collapsing to three fields). The JSON branch
    exists so a `--json`-based implementation would receive well-formed input and
    the tests still fail purely on the piped-stdin defect, not on stub shape.
    """
    rows = "\n".join(f"/dev/{n} {size} {fstype} {devtype}" for n, size, fstype, devtype in devices)
    fstypes = "\n".join(fstype for _n, _s, fstype, _t in devices)
    devices_json = json.dumps({
        "blockdevices": [{
            "name": n, "size": size, "fstype": fstype or None, "type": devtype,
        } for n, size, fstype, devtype in devices],
    })
    return f"""  {disk})
    case "$args" in
      *NAME,SIZE,FSTYPE,TYPE*)
        if [ "${{args#* -J }}" != "$args" ]; then
          cat <<'JSON'
{devices_json}
JSON
        else
          cat <<'ROWS'
{rows}
ROWS
        fi
        ;;
      *FSTYPE*)
        cat <<'FSTYPES'
{fstypes}
FSTYPES
        ;;
    esac
    ;;
"""


def _lsblk_stub_body():
    """Body of a stub `lsblk` answering only the queries the script issues."""
    disk_cases = (
        _device_table_case(_CARD_DISK, _BLOCK_DEVICES)
        + _device_table_case(_UNPROBED_DISK, _UNPROBED_DEVICES)
    )
    return f"""#!/usr/bin/env bash
args=" $* "
disk=""
for a in "$@"; do case "$a" in /dev/*) disk="${{a#/dev/}}" ;; esac; done

# Auto-detection step 1: removable whole disks. Only the fake card is removable,
# so auto-detection has a single candidate.
case "$args" in
  *" NAME,RM "*) printf 'sda 0\\n{_CARD_DISK} 1\\n{_UNPROBED_DISK} 0\\n'; exit 0 ;;
esac

case "$disk" in
{disk_cases}  *)
    printf 'lsblk: %s: not a block device\\n' "$disk" >&2
    exit 1
    ;;
esac
"""


@pytest.fixture
def linux_card(tmp_path):
    """PATH shim making the script see a Linux host with the fake SD inserted."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_stub(stub_bin, "uname", "#!/usr/bin/env bash\necho Linux\n")
    _write_stub(stub_bin, "lsblk", _lsblk_stub_body())
    _write_stub(stub_bin, "sudo", '#!/usr/bin/env bash\nexec "$@"\n')
    # Stub `dd` emits per-device content so each produced image is attributable.
    _write_stub(stub_bin, "dd", (
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in if=*) dev="${a#if=}" ;; esac; done\n'
        "printf 'payload-for-%s\\n' \"$dev\"\n"
    ))
    return stub_bin


def _run(linux_card, tmp_path, *args):
    env = dict(os.environ)
    env["PATH"] = f"{linux_card}{os.pathsep}{env['PATH']}"
    return subprocess.run(  # noqa: S603 - test invokes the pinned helper with fixed args
        ["bash", str(_SCRIPT), "--yes", *args],  # noqa: S607 - bash on PATH is fine in tests
        env=env, cwd=tmp_path, capture_output=True, text=True, check=False,
    )


def _image_bytes(path):
    with gzip.open(path, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Linux partition discovery
# --------------------------------------------------------------------------- #

def test_images_largest_ext_partition_on_linux(linux_card, tmp_path):
    # Guards the Linux discovery backend end-to-end. It previously piped lsblk
    # output into `python3 - <<'PY'`, where the heredoc (the program) *replaces*
    # the pipe as stdin, so the parser read EOF: the run died with
    # "JSONDecodeError: Expecting value: line 1 column 1 (char 0)" right after
    # printing the target disk, producing no image at all. A regression manifests
    # as a non-zero exit with no output file, or as the wrong device being read.
    out = tmp_path / "centaur-sd.img.gz"
    proc = _run(linux_card, tmp_path, "--disk", _CARD_DISK, "--output", str(out))

    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    assert f"/dev/{_ROOT_PART} {_size_of(_ROOT_PART)} {_ROOT_PART}" in proc.stderr
    assert out.is_file()
    assert _image_bytes(out) == _stub_payload(f"/dev/{_ROOT_PART}")


@pytest.mark.parametrize("excluded", [_BOOT_PART, _DATA_PART, _UNFORMATTED_PART, _CARD_DISK])
def test_only_the_ext_root_is_imaged_by_default(linux_card, tmp_path, excluded):
    # The default single-partition run must read exactly the ext4 root: imaging
    # the vfat boot partition, the unformatted partition, or the whole disk gives
    # the importer something it cannot loop-mount as the app filesystem, and the
    # smaller ext4 data partition has no app on it. Manifests as the excluded
    # device appearing in the selection list / as the imaged payload.
    out = tmp_path / "centaur-sd.img.gz"
    proc = _run(linux_card, tmp_path, "--disk", _CARD_DISK, "--output", str(out))

    assert proc.returncode == 0, proc.stderr
    assert f"/dev/{excluded} " not in proc.stderr
    assert _image_bytes(out) != _stub_payload(f"/dev/{excluded}")


def test_autodetect_picks_removable_disk_with_ext_partition(linux_card, tmp_path):
    # Without --disk the script must resolve the card from lsblk's removable
    # devices (sda is non-removable and has no ext partition in the stub table).
    # Manifests as "no SD card with a Linux/ext partition found", a multiple-
    # candidate abort, or the wrong disk being targeted.
    out = tmp_path / "centaur-sd.img.gz"
    proc = _run(linux_card, tmp_path, "--output", str(out))

    assert proc.returncode == 0, proc.stderr
    assert f"Target disk: {_CARD_DISK}" in proc.stderr
    assert _image_bytes(out) == _stub_payload(f"/dev/{_ROOT_PART}")


def test_all_linux_images_every_ext_partition_largest_first(linux_card, tmp_path):
    # --all-linux is the fallback when the app is not on the root partition, so
    # it must image both ext partitions -- numbered largest-first -- and still
    # skip vfat/unformatted/whole-disk devices. Manifests as a missing part file,
    # as the parts being swapped (ordering lost), or as a third part appearing
    # because a non-ext device slipped into the selection.
    out = tmp_path / "centaur-sd.img.gz"
    proc = _run(linux_card, tmp_path, "--disk", _CARD_DISK, "--output", str(out), "--all-linux")

    assert proc.returncode == 0, proc.stderr
    parts = sorted(tmp_path.glob("centaur-sd.part*.img.gz"))
    assert [p.name for p in parts] == ["centaur-sd.part1.img.gz", "centaur-sd.part2.img.gz"]
    assert _image_bytes(parts[0]) == _stub_payload(f"/dev/{_ROOT_PART}")
    assert _image_bytes(parts[1]) == _stub_payload(f"/dev/{_DATA_PART}")
    assert not out.exists()


def test_disk_argument_accepts_a_dev_path(linux_card, tmp_path):
    # `lsblk` prints /dev/sdb, so users paste that into --disk. The script builds
    # "/dev/${disk}" internally, which turned that into /dev//dev/sdb and failed
    # with "no Linux/ext partition found". Manifests as a non-zero exit here.
    out = tmp_path / "centaur-sd.img.gz"
    proc = _run(linux_card, tmp_path, "--disk", f"/dev/{_CARD_DISK}", "--output", str(out))

    assert proc.returncode == 0, proc.stderr
    assert _image_bytes(out) == _stub_payload(f"/dev/{_ROOT_PART}")


# --------------------------------------------------------------------------- #
# Failure reporting
# --------------------------------------------------------------------------- #

def test_unknown_disk_reports_actionable_error(linux_card, tmp_path):
    # When lsblk fails (wrong/absent disk), the user must get the script's own
    # guidance, not a raw traceback or a bare pipefail exit. Manifests as an
    # interpreter traceback or an empty stderr with a non-zero status.
    proc = _run(linux_card, tmp_path, "--disk", "sdz", "--output", str(tmp_path / "x.img.gz"))

    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "no Linux/ext partition found on sdz" in proc.stderr


def test_unidentifiable_filesystems_suggest_rerunning_with_sudo(linux_card, tmp_path):
    # lsblk leaves FSTYPE empty when it has neither udev data nor read access to
    # the device, so a card that does hold ext4 presents as having no ext
    # partition. Without the hint the user hits a dead end that reads as "wrong
    # card". Manifests as the bare "not found" message with no next step.
    proc = _run(linux_card, tmp_path, "--disk", _UNPROBED_DISK,
                "--output", str(tmp_path / "x.img.gz"))

    assert proc.returncode != 0
    assert f"no Linux/ext partition found on {_UNPROBED_DISK}" in proc.stderr
    assert "re-run with sudo" in proc.stderr
    assert not list(tmp_path.glob("*.img.gz"))


def test_discovery_never_pipes_data_into_a_heredoc_program():
    # `cmd | python3 - <<'PY'` is the defect above: the heredoc supplies stdin,
    # so the piped data is discarded and the program parses EOF. Guards against
    # reintroducing that shape anywhere in the script; manifests as an
    # empty-input parse error at runtime, which no static check would catch.
    text = _SCRIPT.read_text()
    assert not re.search(r"\|[^\n]*python3\s+-(?:\s|$)", text)

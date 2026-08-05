#!/usr/bin/env bash
#
# make-centaur-image.sh
#
# Capture the original DGT Centaur SD card's Linux (ext4) root partition into a
# compressed image that can be uploaded to Universal Chess (System -> Original
# Centaur -> Import from SD).
#
# Why an image instead of a plain file copy: the Centaur application lives on an
# ext4 partition. macOS cannot mount ext4 at all, but it CAN read the
# partition's raw bytes, so this script dd's the partition into a gzip image.
# Universal Chess runs on Linux and loop-mounts that image read-only to extract
# the app. The same dd path also works on Linux, so one script serves both.
#
# The SD is only ever read (never written), so it is safe to run against a
# read-only card. The image is the partition, not the whole 4 GB disk, so the
# upload is ~200 MB (gzip collapses the partition's free space).
#
# Usage:
#   ./make-centaur-image.sh [--disk <id>] [--output <file>] [--yes] [--all-linux]
#
#   --disk <id>     Target whole-disk identifier (e.g. disk8 on macOS, sdb on
#                   Linux). Skips auto-detection.
#   --output <file> Output image path (default: ./centaur-sd.img.gz).
#   --yes           Do not prompt for confirmation.
#   --all-linux     Image every Linux partition found (root + data), not just
#                   the largest. Use only if import cannot find the app on the
#                   root partition.
#   -h, --help      Show this help.

set -euo pipefail

OUTPUT="centaur-sd.img.gz"
TARGET_DISK=""
ASSUME_YES=0
ALL_LINUX=0

die() { echo "error: $*" >&2; exit 1; }
note() { echo ">> $*" >&2; }

# Print the leading comment banner (every '#' line after the shebang, up to the
# first blank/non-comment line) so help text and this header never drift apart.
usage() { awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --disk) TARGET_DISK="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    --all-linux) ALL_LINUX=1; shift ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

OS="$(uname -s)"

# `lsblk` and `diskutil list` both display devices as /dev paths, so accept a
# pasted "/dev/sdb" for --disk: the discovery backends build "/dev/${disk}"
# themselves and would otherwise look for /dev//dev/sdb.
TARGET_DISK="${TARGET_DISK#/dev/}"

# sha256 helper differs by platform.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "(sha256 tool not found)"
  fi
}

# ---------------------------------------------------------------------------
# Partition discovery. Each backend prints, one per line:
#   <raw-partition-device> <size-bytes> <human-label>
# sorted so the FIRST line is the largest Linux partition (the ext4 root that
# holds the app -- the smaller Linux partition is persistent data).
# ---------------------------------------------------------------------------

discover_macos() {
  local disk="$1" line ident bytes
  # `diskutil list <disk>` rows look like:
  #   2:   Linux   209.7 MB   disk8s2
  # The type token is "Linux" for ext partitions (macOS has no ext driver).
  diskutil list "$disk" 2>/dev/null | awk '/[[:space:]]Linux[[:space:]]/{print $NF}' | while read -r ident; do
    [ -n "$ident" ] || continue
    # Disk Size line: "Disk Size: 209.7 MB (209715200 Bytes) (...)"
    bytes="$(diskutil info "$ident" 2>/dev/null | awk -F'[()]' '/Disk Size/{print $2}' | awk '{print $1}')"
    [ -n "$bytes" ] || continue
    echo "/dev/r${ident} ${bytes} ${ident}"
  done | sort -k2 -nr
}

discover_linux() {
  local disk="$1"
  # `lsblk --list` prints one flat row per device with no tree glyphs, and
  # --paths prefixes /dev, so awk reads fixed columns: NAME SIZE FSTYPE TYPE.
  # Devices with no filesystem yield an empty FSTYPE and therefore only three
  # fields; requiring TYPE=="part" in $4 skips those rows (and the whole-disk
  # row, which must never be imaged) without a false ext match.
  lsblk -b -l -n -p -o NAME,SIZE,FSTYPE,TYPE "/dev/${disk}" 2>/dev/null |
    awk '$4 == "part" && $3 ~ /^ext/ { name = $1; sub(/^\/dev\//, "", name); print $1, $2, name }' |
    sort -k2 -nr
}

# Auto-detect the SD whole-disk if not given.
autodetect_disk() {
  if [ "$OS" = "Darwin" ]; then
    # External, physical disks that contain at least one Linux partition.
    local d found=""
    for d in $(diskutil list 2>/dev/null | awk '/\(external, physical\)/{print $1}'); do
      if diskutil list "$d" 2>/dev/null | grep -q "[[:space:]]Linux[[:space:]]"; then
        found="${found} ${d##*/}"
      fi
    done
    echo "$found" | tr -s ' ' '\n' | sed '/^$/d'
  else
    # Removable block devices that have an ext* partition.
    local n
    for n in $(lsblk -dn -o NAME,RM 2>/dev/null | awk '$2==1{print $1}'); do
      if lsblk -n -o FSTYPE "/dev/${n}" 2>/dev/null | grep -q '^ext'; then
        echo "$n"
      fi
    done
  fi
}

if [ -z "$TARGET_DISK" ]; then
  note "Auto-detecting the Centaur SD card..."
  candidates="$(autodetect_disk || true)"
  count="$(printf '%s\n' "$candidates" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [ "$count" = "0" ]; then
    die "no SD card with a Linux/ext partition found. Insert the card and pass --disk <id> (see: $( [ "$OS" = Darwin ] && echo 'diskutil list' || echo 'lsblk' ))."
  elif [ "$count" != "1" ]; then
    die "multiple candidate disks found ($candidates). Re-run with --disk <id> to choose."
  fi
  TARGET_DISK="$(printf '%s\n' "$candidates" | sed '/^$/d' | head -n1)"
fi

note "Target disk: ${TARGET_DISK}"

# `|| true` keeps a failing probe (absent or mistyped disk) on the friendly
# "no Linux/ext partition found" path instead of aborting via pipefail/errexit.
if [ "$OS" = "Darwin" ]; then
  parts="$(discover_macos "$TARGET_DISK" || true)"
else
  parts="$(discover_linux "$TARGET_DISK" || true)"
fi

if [ -z "$parts" ]; then
  # lsblk takes FSTYPE from udev and otherwise probes the device itself, which
  # needs read access. With neither available every partition lists with an
  # empty FSTYPE, so a card that does hold ext4 looks like it has none.
  hint=""
  [ "$OS" = "Darwin" ] || hint=" If the card does have one, re-run with sudo so lsblk can read the filesystem types."
  die "no Linux/ext partition found on ${TARGET_DISK}.${hint}"
fi

if [ "$ALL_LINUX" = "1" ]; then
  selected="$parts"
else
  # Largest Linux partition = ext4 root holding the app.
  selected="$(printf '%s\n' "$parts" | head -n1)"
fi

echo "" >&2
note "Partitions to image (device  bytes  name):"
printf '%s\n' "$selected" | sed 's/^/   /' >&2
echo "" >&2

if [ "$ASSUME_YES" != "1" ]; then
  printf 'Read the above partition(s) into %s ? [y/N] ' "$OUTPUT" >&2
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) die "aborted." ;; esac
fi

# ---------------------------------------------------------------------------
# Image it. Single partition -> OUTPUT directly. Multiple (--all-linux) ->
# OUTPUT plus a numbered suffix per partition, since each is a separate ext4.
# bs is given in plain bytes (4 MiB) which both BSD (macOS) and GNU dd accept.
# ---------------------------------------------------------------------------
BS=4194304

image_one() {
  local dev="$1" out="$2"
  note "Reading ${dev} -> ${out} (this can take a few minutes; sudo may prompt)..."
  # Raw read of an unmounted partition: read-only, no ext4 driver needed.
  sudo dd if="$dev" bs="$BS" 2>/dev/null | gzip -c > "$out"
  local sz
  sz="$(wc -c < "$out" | tr -d ' ')"
  [ "$sz" -gt 0 ] || die "produced an empty image (${out}); check the device and sudo access."
  note "Wrote ${out} (${sz} bytes compressed)."
  note "SHA-256: $(sha256_of "$out")"
}

n="$(printf '%s\n' "$selected" | wc -l | tr -d ' ')"
if [ "$n" = "1" ]; then
  dev="$(printf '%s\n' "$selected" | awk '{print $1}')"
  image_one "$dev" "$OUTPUT"
  produced="$OUTPUT"
else
  produced=""
  i=1
  while read -r dev _bytes _name; do
    [ -n "$dev" ] || continue
    out="${OUTPUT%.img.gz}.part${i}.img.gz"
    image_one "$dev" "$out"
    produced="${produced} ${out}"
    i=$((i+1))
  done <<EOF
$selected
EOF
fi

echo "" >&2
note "Done. Upload via Universal Chess: System -> Original Centaur Software -> Import from SD."
note "File(s) to upload:${produced:+ }${produced:-$OUTPUT}"

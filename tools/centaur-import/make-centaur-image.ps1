<#
.SYNOPSIS
    Capture the original DGT Centaur SD card's Linux (ext4) partition into a
    compressed image for upload to Universal Chess (System -> Original Centaur
    -> Import from SD). Windows equivalent of make-centaur-image.sh.

.DESCRIPTION
    The Centaur application lives on an ext4 partition. Windows cannot mount
    ext4, but it CAN read the partition's raw bytes from the physical disk, so
    this script reads that partition and writes a gzip image. Universal Chess
    runs on Linux and loop-mounts the image read-only to extract the app.

    The card is only ever READ (never written), so it is safe against a
    read-only card. The image is the partition, not the whole disk, so gzip
    collapses the partition's free space to a ~200 MB upload.

    Must be run from an elevated (Administrator) PowerShell: reading a raw
    physical disk requires administrative rights.

.PARAMETER DiskNumber
    Target disk number (as shown by Get-Disk, e.g. 2). Skips auto-detection.

.PARAMETER Output
    Output image path. Default: .\centaur-sd.img.gz

.PARAMETER Yes
    Do not prompt for confirmation.

.PARAMETER AllLinux
    Image every Linux partition found (root + data), not just the largest. Use
    only if import cannot find the app on the root partition.

.EXAMPLE
    .\make-centaur-image.ps1
    Auto-detect the SD card and image its ext4 root partition.

.EXAMPLE
    .\make-centaur-image.ps1 -DiskNumber 2 -Output C:\temp\centaur-sd.img.gz -Yes
#>
[CmdletBinding()]
param(
    [int] $DiskNumber = -1,
    [string] $Output = "centaur-sd.img.gz",
    [switch] $Yes,
    [switch] $AllLinux
)

$ErrorActionPreference = "Stop"

# Linux ext partitions are MBR type 0x83 (131) or the GPT "Linux filesystem
# data" type GUID. The Centaur SD is an MBR Raspberry Pi image, but both are
# handled so a re-imaged card on either scheme works.
$MbrLinuxType = 131
$GptLinuxGuid = "{0FC63DAF-8483-4772-8E79-3D69D8477DE4}"
$SectorSize = 512
$BufferBytes = 4MB   # multiple of the sector size; raw device reads must align

function Note([string] $msg) { Write-Host ">> $msg" }
function Die([string] $msg)  { Write-Error $msg; exit 1 }

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Die "This script must be run as Administrator (reading a raw disk requires it). Right-click PowerShell -> 'Run as administrator', then re-run."
    }
}

# Parse the MBR partition table directly from the raw disk as a fallback for
# when Get-Partition does not classify the ext partition's type. Returns the
# same shape as Get-LinuxPartitions: objects with Number/Offset/Size/Label.
function Read-MbrLinuxPartitions([int] $disk) {
    $device = "\\.\PhysicalDrive$disk"
    $fs = $null
    try {
        $fs = New-Object System.IO.FileStream($device, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite, 1, [IO.FileOptions]::None)
        $mbr = New-Object byte[] $SectorSize
        if ($fs.Read($mbr, 0, $SectorSize) -ne $SectorSize) { return @() }
    } catch {
        return @()
    } finally {
        if ($fs) { $fs.Dispose() }
    }
    # Boot signature 0x55AA marks a valid MBR; without it there is no table.
    if ($mbr[510] -ne 0x55 -or $mbr[511] -ne 0xAA) { return @() }

    $found = @()
    for ($i = 0; $i -lt 4; $i++) {
        $entry = 446 + ($i * 16)
        $type = $mbr[$entry + 4]
        if ($type -ne $MbrLinuxType) { continue }
        # LBA start and sector count are little-endian 32-bit at +8 and +12.
        $lba = [BitConverter]::ToUInt32($mbr, $entry + 8)
        $count = [BitConverter]::ToUInt32($mbr, $entry + 12)
        if ($count -eq 0) { continue }
        $found += [pscustomobject]@{
            Number = $i + 1
            Offset = [uint64]$lba * $SectorSize
            Size   = [uint64]$count * $SectorSize
            Label  = "partition $($i + 1) (MBR type 0x83)"
        }
    }
    return $found
}

# All Linux ext partitions on the disk, largest first. Prefers Get-Partition
# (uniform byte Offset/Size for MBR and GPT); falls back to a raw MBR parse.
function Get-LinuxPartitions([int] $disk) {
    $rows = @()
    try {
        foreach ($p in Get-Partition -DiskNumber $disk -ErrorAction Stop) {
            $isLinux = $false
            if ($null -ne $p.MbrType -and [int]$p.MbrType -eq $MbrLinuxType) { $isLinux = $true }
            if ($null -ne $p.GptType -and $p.GptType -eq $GptLinuxGuid) { $isLinux = $true }
            if (-not $isLinux) { continue }
            $rows += [pscustomobject]@{
                Number = $p.PartitionNumber
                Offset = [uint64]$p.Offset
                Size   = [uint64]$p.Size
                Label  = "partition $($p.PartitionNumber)"
            }
        }
    } catch {
        $rows = @()
    }
    if ($rows.Count -eq 0) { $rows = Read-MbrLinuxPartitions $disk }
    return @($rows | Sort-Object -Property Size -Descending)
}

# Disks that look like an inserted SD card (removable / USB / SD bus) and carry
# at least one Linux partition.
function Find-CandidateDisks {
    $candidates = @()
    foreach ($d in Get-Disk -ErrorAction SilentlyContinue) {
        $removable = ($d.BusType -in @("USB", "SD", "MMC")) -or ($d.IsRemovable -eq $true)
        if (-not $removable) { continue }
        if ((Get-LinuxPartitions $d.Number).Count -gt 0) { $candidates += $d.Number }
    }
    return @($candidates)
}

function Read-PartitionToGzip([int] $disk, [uint64] $offset, [uint64] $size, [string] $outPath) {
    $device = "\\.\PhysicalDrive$disk"
    Note "Reading $device offset=$offset size=$size -> $outPath (this can take a few minutes)..."

    $src = $null; $dst = $null; $gz = $null
    try {
        # bufferSize 1 disables FileStream's internal buffering so every Read
        # issues a single, sector-aligned ReadFile straight to the device (raw
        # disk handles require aligned offset+length).
        $src = New-Object System.IO.FileStream($device, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite, 1, [IO.FileOptions]::None)
        [void]$src.Seek([int64]$offset, [IO.SeekOrigin]::Begin)

        $dst = New-Object System.IO.FileStream($outPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $gz = New-Object System.IO.Compression.GZipStream($dst, [System.IO.Compression.CompressionMode]::Compress)

        $buffer = New-Object byte[] $BufferBytes
        [uint64]$remaining = $size
        [uint64]$done = 0
        $lastPct = -1
        while ($remaining -gt 0) {
            $want = [int][Math]::Min([uint64]$BufferBytes, $remaining)
            $read = $src.Read($buffer, 0, $want)
            if ($read -le 0) { Die "unexpected end of device before the partition was fully read (read $done of $size bytes)." }
            $gz.Write($buffer, 0, $read)
            $remaining -= [uint64]$read
            $done += [uint64]$read
            $pct = [int](($done * 100) / $size)
            if ($pct -ne $lastPct -and ($pct % 5 -eq 0)) {
                Write-Progress -Activity "Imaging $device" -Status "$pct% ($done / $size bytes)" -PercentComplete $pct
                $lastPct = $pct
            }
        }
    } finally {
        if ($gz)  { $gz.Dispose() }   # flushes the gzip trailer before the file closes
        if ($dst) { $dst.Dispose() }
        if ($src) { $src.Dispose() }
        Write-Progress -Activity "Imaging $device" -Completed
    }

    $compressed = (Get-Item $outPath).Length
    if ($compressed -le 0) { Die "produced an empty image ($outPath); check the disk number and that PowerShell is elevated." }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $outPath).Hash
    Note "Wrote $outPath ($compressed bytes compressed)."
    Note "SHA-256: $hash"
}

# --- main --------------------------------------------------------------------

Assert-Administrator

if ($DiskNumber -lt 0) {
    Note "Auto-detecting the Centaur SD card..."
    $found = Find-CandidateDisks
    if ($found.Count -eq 0) {
        Die "no removable disk with a Linux/ext partition found. Insert the card and pass -DiskNumber <n> (see: Get-Disk)."
    } elseif ($found.Count -gt 1) {
        Die "multiple candidate disks found ($($found -join ', ')). Re-run with -DiskNumber <n> to choose."
    }
    $DiskNumber = $found[0]
}

Note "Target disk: $DiskNumber"

$parts = Get-LinuxPartitions $DiskNumber
if ($parts.Count -eq 0) {
    Die "no Linux/ext partition found on disk $DiskNumber. Confirm the disk number with Get-Disk / Get-Partition."
}

if (-not $AllLinux) {
    # Largest Linux partition = the ext4 root that holds the app.
    $parts = @($parts[0])
}

Write-Host ""
Note "Partition(s) to image:"
$parts | ForEach-Object { Write-Host ("   {0}  offset={1}  size={2} bytes" -f $_.Label, $_.Offset, $_.Size) }
Write-Host ""

if (-not $Yes) {
    $reply = Read-Host "Read the above partition(s) into $Output ? [y/N]"
    if ($reply -notmatch '^(y|yes)$') { Die "aborted." }
}

$produced = @()
if ($parts.Count -eq 1) {
    Read-PartitionToGzip $DiskNumber $parts[0].Offset $parts[0].Size $Output
    $produced += $Output
} else {
    $base = $Output -replace '\.img\.gz$', ''
    $idx = 1
    foreach ($p in $parts) {
        $out = "$base.part$idx.img.gz"
        Read-PartitionToGzip $DiskNumber $p.Offset $p.Size $out
        $produced += $out
        $idx++
    }
}

Write-Host ""
Note "Done. Upload via Universal Chess: System -> Original Centaur Software -> Import from SD."
Note "File(s) to upload: $($produced -join ', ')"

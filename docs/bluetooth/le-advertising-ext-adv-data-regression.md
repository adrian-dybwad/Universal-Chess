# BlueZ Bug Report: LE Advertising Fails on Kernel ≥ 6.18 (`Add Extended Advertising Data` length mismatch)

> **Status (2026-06-19): root cause confirmed end-to-end at the byte level.**
> This is a real BlueZ defect, already fixed upstream on `master` but not yet in
> any released version, exposed by a new kernel validation patch. Diagnosed on a
> disposable test board; fix not yet deployed to the fleet.

## Summary

After a kernel upgrade, the board can no longer advertise over BLE. Every attempt
to register an advertisement fails. The board is then undiscoverable by chess
apps even though Bluetooth otherwise connects.

`bluetoothd` reports:

```
src/advertising.c:add_client_complete() Failed to add advertisement: Invalid Parameters (0x0d)
org.bluez.Error.Failed: Failed to register advertisement
```

The cause is a length-field mismatch: BlueZ sends an `Add Extended Advertising
Data` MGMT command that is **8 bytes larger** than the lengths it declares, and a
new kernel patch now rejects that mismatch. Older kernels silently tolerated the
extra bytes, which is why this surfaced only after a kernel upgrade.

## Environment

| | Working | Failing |
|---|---|---|
| Kernel | `6.12.x+rpt-rpi-v7` (e.g. 6.12.75) | `6.18.34+rpt-rpi-v7` |
| BlueZ | `5.82-1.1+rpt1` | `5.82-1.1+rpt1` (identical) |
| Controller | Broadcom BCM43430 (Pi Zero 2 W) | Broadcom BCM43430 (Pi Zero 2 W) |
| LE advertising | Works | Fails (`Invalid Parameters 0x0d`) |

BlueZ is identical on both; **only the kernel differs**. The controller/chip
stepping was investigated and ruled out as causal — the same BlueZ command fails
purely as a function of kernel version.

> Note: on kernel 6.18.x this combo chip also showed unrelated WiFi
> (`brcmfmac`/SDIO) instability. That is a separate driver regression and is not
> part of this report.

## Root Cause

Two independent changes collide.

### BlueZ side — sizes the command with the wrong struct

`src/advertising.c`, `add_adv_params_callback()` builds the
`MGMT_OP_ADD_EXT_ADV_DATA` command but computes its length using the **legacy**
`mgmt_cp_add_advertising` struct instead of the extended `mgmt_cp_add_ext_adv_data`
struct it actually fills in:

```c
struct mgmt_cp_add_ext_adv_data *cp = NULL;   /* the real command */
...
/* BUG: wrong struct — sizeof is 11, not 3 */
param_len = sizeof(struct mgmt_cp_add_advertising) + adv_data_len + scan_rsp_len;
cp = malloc0(param_len);                       /* zero-filled */
...
mgmt_send(..., MGMT_OP_ADD_EXT_ADV_DATA, ..., param_len, cp, ...);
```

- `sizeof(struct mgmt_cp_add_advertising)` = `instance`(1) + `flags`(4) +
  `duration`(2) + `timeout`(2) + `adv_data_len`(1) + `scan_rsp_len`(1) = **11**
- `sizeof(struct mgmt_cp_add_ext_adv_data)` = `instance`(1) + `adv_data_len`(1) +
  `scan_rsp_len`(1) = **3**

`malloc0` zero-fills, so the 8-byte difference (`flags`+`duration`+`timeout`) is
sent as **8 trailing zero bytes** that the command's own length fields do not
account for.

### Kernel side — now validates the length

Kernel commit `d3f7d17960ed` ("Bluetooth: MGMT: validate Add Extended
Advertising Data length", 2026-05-15, in 6.18 and backported to other stable
trees) rejects any command whose total length does not equal the fixed header
plus both declared data lengths:

```c
expected_len = struct_size(cp, data, cp->adv_data_len + cp->scan_rsp_len);
if (expected_len != data_len)
    return mgmt_cmd_status(sk, hdev->id, MGMT_OP_ADD_EXT_ADV_DATA,
                           MGMT_STATUS_INVALID_PARAMS);
```

For BlueZ's command, `expected_len = 3 + adv_data_len + scan_rsp_len` but
`data_len` carries the extra 8 bytes, so the check fails. The patch was written
to close an out-of-bounds read for *short* commands (KASAN reported an 8-byte
slab OOB read); BlueZ's *over-long* command trips the same strict-equality test.

## Byte-Level Evidence

Captured with `btmon -C 240` on kernel 6.18.34, minimal advertisement
(`Type=peripheral`, `LocalName=UCTEST`, no UUIDs / no manufacturer data):

```
@ MGMT Command: Add Extended Advertising Data (0x0055) plen 19
        Instance: 1
        Advertising data length: 0
        Scan response length: 8
        Name (complete): UCTEST
@ MGMT Event: Command Status (0x0002) plen 3
      Add Extended Advertising Data (0x0055)
        Status: Invalid Parameters (0x0d)
```

Raw command parameters (19 bytes) from the btsnoop capture:

```
01 00 08 07 09 55 43 54 45 53 54 | 00 00 00 00 00 00 00 00
└instance, adv_len=0, scan_len=8, scan-rsp AD "UCTEST"   └─ 8 trailing ZERO bytes
```

- Declared content = header(3) + `adv_data_len`(0) + `scan_rsp_len`(8) = **11**
- Actual command `plen` = **19** → 8-byte overhang of zeros

The overhang is **constant 8 bytes**, independent of payload — confirming it is a
fixed header-size error, not content/serialization padding:

| LocalName | `scan_rsp_len` | declared (3 + scan) | actual `plen` | overhang |
|---|---|---|---|---|
| `UCTEST` (6 chars) | 8 | 11 | 19 | **8** |
| `ABCDEFGHIJKLMNOP` (16 chars) | 18 | 21 | 29 | **8** |

A minimal D-Bus probe (no `ServiceUUIDs`, no `ManufacturerData`) still produced
the 8 zero bytes, ruling out D-Bus serialization / UUID / manufacturer-data
padding as the cause.

## Upstream Status

- **BlueZ fix (not yet released):** commit
  `2a6968b40378dca5650e18e03ad0407738c47be5` — "advertising: Fix sending extra
  bytes with MGMT_OP_ADD_EXT_ADV_DATA" (Luiz Augusto von Dentz, 2026-06-02).
  Changes the line to `param_len = sizeof(*cp) + adv_data_len + scan_rsp_len;`.
  On `master` only; **not** in 5.82 / 5.83 / 5.84, so `apt` has no fixed
  package.
- **Kernel patch that exposes it:** commit
  `d3f7d17960ed50df3a6709c5158caff989c8c905` — "Bluetooth: MGMT: validate Add
  Extended Advertising Data length" (2026-05-15), in 6.18 and backported.

The BlueZ fix landed ~2 weeks after the kernel patch; the kernel tightened
validation first, and no released BlueZ yet carries the corresponding fix.

## Impact

- No BLE advertising on any kernel that contains the validation patch
  (6.18.x, and backports to other stable trees). The board cannot be discovered
  by chess apps.
- Triggered by the blanket `sudo apt upgrade -y` step in our update/CI flow,
  which pulled the newer kernel. That instruction is a contributing factor and
  should be revisited.

## Candidate Solutions (not yet applied)

1. **Patch BlueZ (root-cause fix).** Backport the one-line upstream change
   (`sizeof(*cp)`) onto `5.82-1.1+rpt1`, rebuild the package, install, and
   `apt-mark hold bluez`. Keeps the kernel security patch. Cost: maintaining a
   forked package until Raspberry Pi ships a BlueZ ≥ the fix.
2. **Pin the kernel.** Hold a `6.12.x` image (proven working) and stop the
   blanket `apt upgrade -y`. Simple and proven, but stays on an older kernel
   without the validation patch and is brittle once the patch is backported to
   6.12.x.

Recommended: option 1 (matches upstream's own fix) for boards that must run the
newer kernel; option 2 as an immediate stopgap.

## Reproduction

On a board running a kernel with commit `d3f7d17` and BlueZ ≤ 5.82, register any
LE advertisement via the `org.bluez.LEAdvertisingManager1` D-Bus API and observe
`org.bluez.Error.Failed: Failed to register advertisement`, with `btmon` showing
`Add Extended Advertising Data (0x0055)` returning `Invalid Parameters (0x0d)`.

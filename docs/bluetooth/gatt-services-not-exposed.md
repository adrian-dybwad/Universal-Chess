# BlueZ Bug Report: GATT Services Not Exposed via D-Bus for Dual-Mode Device

> **Status (2026-06-19): root cause reassessed — this is a peer-device firmware
> defect, not a BlueZ parser bug.** The original "Suspected Cause" (a BlueZ
> byte-offset/endianness error) was disproven by reading the BlueZ 5.82 source
> and decoding the captured bytes. See
> [Root Cause Analysis (2026-06-19)](#root-cause-analysis-2026-06-19). The
> sections above are preserved as the original investigation record.

## Summary

BlueZ 5.82 successfully connects to a dual-mode Bluetooth device (Millennium ChessLink) via LE transport and performs GATT service discovery at the HCI level, but fails to expose the discovered services via D-Bus. The `ServicesResolved` property becomes `1` (true), but the `UUIDs` property remains empty and no GATT service/characteristic D-Bus objects are created.

The same device works correctly with `gatttool` interactive mode, which performs GATT discovery directly without going through D-Bus.

## Environment

- **BlueZ version**: 5.82
- **Kernel**: Linux (Raspberry Pi OS, Debian-based)
- **Bluetooth adapter**: BCM43438 (Raspberry Pi built-in)
- **Device**: Millennium ChessLink chess board (dual-mode: BR/EDR + BLE)
- **Device address**: 34:81:F4:ED:78:34 (public)

## Steps to Reproduce

1. Ensure the device is not paired via classic Bluetooth
2. Enable `Experimental = true` in `/etc/bluetooth/main.conf` (required for `PreferredBearer`)
3. Restart bluetoothd
4. Run the following Python script:

```python
import dbus
import time

bus = dbus.SystemBus()

# Start LE-only discovery
adapter = dbus.Interface(
    bus.get_object('org.bluez', '/org/bluez/hci0'),
    'org.bluez.Adapter1'
)
adapter.SetDiscoveryFilter({'Transport': dbus.String('le')})
adapter.StartDiscovery()
time.sleep(10)

device_path = '/org/bluez/hci0/dev_34_81_F4_ED_78_34'

# Set PreferredBearer to force LE connection
device_props = dbus.Interface(
    bus.get_object('org.bluez', device_path),
    'org.freedesktop.DBus.Properties'
)
device_props.Set('org.bluez.Device1', 'PreferredBearer', dbus.String('le'))

# Connect
device = dbus.Interface(
    bus.get_object('org.bluez', device_path),
    'org.bluez.Device1'
)
device.Connect()
print('Connected')

# Wait for services to resolve
for i in range(15):
    time.sleep(1)
    resolved = device_props.Get('org.bluez.Device1', 'ServicesResolved')
    uuids = list(device_props.Get('org.bluez.Device1', 'UUIDs'))
    print(f'{i+1}s: ServicesResolved={resolved}, UUIDs={len(uuids)}')
    if len(uuids) > 0:
        print(f'UUIDs: {uuids}')
        break

# Check for GATT D-Bus objects
manager = dbus.Interface(
    bus.get_object('org.bluez', '/'),
    'org.freedesktop.DBus.ObjectManager'
)
objects = manager.GetManagedObjects()
gatt_objects = [p for p in objects.keys() if device_path in p and '/service' in p]
print(f'GATT service objects: {gatt_objects}')

device.Disconnect()
adapter.StopDiscovery()
```

## Expected Behavior

After `ServicesResolved` becomes `1`:
- `UUIDs` property should contain the discovered service UUIDs
- D-Bus objects should be created under the device path for services and characteristics (e.g., `/org/bluez/hci0/dev_34_81_F4_ED_78_34/service0001`)

## Actual Behavior

```
Connected
1s: ServicesResolved=0, UUIDs=0
2s: ServicesResolved=1, UUIDs=0
3s: ServicesResolved=1, UUIDs=0
...
15s: ServicesResolved=1, UUIDs=0
GATT service objects: []
```

- `ServicesResolved` becomes `1` after ~1 second
- `UUIDs` remains empty (`[]`)
- No GATT D-Bus objects are created

## btmon Capture

Running `btmon` during the connection shows that GATT discovery **is happening** at the HCI level:

```
ATT: Exchange MTU Request (0x02) len 2
  Client RX MTU: 517
ATT: Exchange MTU Response (0x03) len 2
  Server RX MTU: 160
ATT: Read By Group Type Request (0x10) len 6
  Handle range: 0x0001-0xffff
  Attribute group type: Primary Service (0x2800)
ATT: Read By Group Type Response (0x11) len 13
  Attribute data length: 6
  Attribute group list: 2 entries
  Handle range: 0x0001-0x0007
  UUID: Generic Access Profile (0x1800)
  Handle range: 0x0010-0x0020
  UUID: Device Information (0x180a)
ATT: Read By Group Type Response (0x11) len 21
  Attribute data length: 20
  Attribute group list: 1 entry
  Handle range: 0x0030-0x003d
  UUID: Vendor specific (49535343-fe7d-4ae5-8fa9-9fafd205e455)
```

The services are discovered correctly:
- Generic Access Profile (0x1800)
- Device Information (0x180a)
- Vendor specific service (49535343-fe7d-4ae5-8fa9-9fafd205e455)

However, the characteristic discovery response appears to be parsed incorrectly:

```
ATT: Read By Type Response (0x09) len 141
  Attribute data length: 21
  Attribute data list: 6 entries
  Handle: 0x0002
  Value[19]: 020300002a0400020500012a0600020700042a
      Properties: 0x02
        Read (0x02)
      Value Handle: 0x0003
      Value UUID: Vendor specific (2a040007-0200-062a-0100-050200042a00)
```

The `Value UUID` shown is malformed (`2a040007-0200-062a-0100-050200042a00`). This should be parsing short 16-bit UUIDs like `0x2a00`, `0x2a01`, etc., but BlueZ appears to be reading the wrong byte offsets.

## Workaround

Using `gatttool` in interactive mode works correctly:

```bash
$ gatttool -b 34:81:F4:ED:78:34 -I
[34:81:F4:ED:78:34][LE]> connect
Attempting to connect to 34:81:F4:ED:78:34
Connection successful
[34:81:F4:ED:78:34][LE]> primary
attr handle: 0x0001, end grp handle: 0x0007 uuid: 00001800-0000-1000-8000-00805f9b34fb
attr handle: 0x0010, end grp handle: 0x0020 uuid: 0000180a-0000-1000-8000-00805f9b34fb
attr handle: 0x0030, end grp handle: 0x003d uuid: 49535343-fe7d-4ae5-8fa9-9fafd205e455
[34:81:F4:ED:78:34][LE]> char-desc
handle: 0x0037, uuid: 0000fff2-0000-1000-8000-00805f9b34fb
handle: 0x003a, uuid: 0000fff1-0000-1000-8000-00805f9b34fb
...
```

`gatttool` correctly discovers the services and characteristics, including the `fff1` and `fff2` characteristics that the D-Bus layer fails to expose.

## Additional Notes

1. **Device is dual-mode**: The device advertises both BR/EDR (Serial Port Profile) and BLE. Without setting `PreferredBearer` to `le`, BlueZ attempts a BR/EDR connection and fails with `br-connection-profile-unavailable`.

2. **Connection is established**: `hcitool con` confirms an LE connection is active:
   ```
   < LE 34:81:F4:ED:78:34 handle 64 state 1 lm CENTRAL
   ```

3. **GATT Client is enabled**: `/etc/bluetooth/main.conf` has:
   ```
   [GATT]
   Client = true
   ReverseServiceDiscovery = true
   ```

4. **Cache was cleared**: The issue persists after removing `/var/lib/bluetooth/*/cache/*` and restarting bluetoothd.

5. **Other BLE devices work**: A Chessnut Air board (BLE-only) connects and has its GATT services properly exposed via D-Bus using the same code.

## Suspected Cause (original hypothesis — superseded)

The original hypothesis was that BlueZ's GATT client was mis-parsing valid ATT
responses (a byte-offset or endianness bug in the ATT response parser), based on
the malformed UUID `2a040007-0200-062a-0100-050200042a00` seen in the btmon
output. This hypothesis is **superseded** by the analysis below.

## Root Cause Analysis (2026-06-19)

The BlueZ parser is correct. The malformed result is caused by the **device
sending a `Read By Type Response` with an incorrect "Attribute data length"
field** (21 instead of 7). BlueZ parses strictly to the ATT spec, trusting that
length, and therefore reads garbage. Evidence:

### 1. The parser is spec-correct

`src/shared/gatt-helpers.c`, `bt_gatt_iter_next_characteristic()` (BlueZ 5.82):

```c
/*
 * Data length contains 7 or 21 octets:
 * 2 octets: Attribute handle
 * 1 octet:  Characteristic properties
 * 2 octets: Characteristic value handle
 * 2 or 16 octets: characteristic UUID
 */
if (iter->result->data_len != 21 && iter->result->data_len != 7)
    return false;

*start_handle  = get_le16(pdu_ptr);
*properties    = ((uint8_t *) pdu_ptr)[2];
*value_handle  = get_le16(pdu_ptr + 3);
convert_uuid_le(pdu_ptr + 5, iter->result->data_len - 5, uuid);
iter->pos += iter->result->data_len;
```

`data_len` is taken directly from the device's response length byte
(`data_length = ((const uint8_t *) pdu)[0];`) and validated only with
`(length - 1) % data_length == 0`. The UUID size is derived as `data_len - 5`
(2 for a 16-bit UUID record of 7, 16 for a 128-bit UUID record of 21). The byte
offsets are correct per the ATT specification.

### 2. The malformed UUID decodes exactly as a device-declared 21-byte record

From the capture: `data_len = 21`, `Handle: 0x0002`,
`Value[19] = 02 03 00 00 2a 04 00 02 05 00 01 2a 06 00 02 07 00 04 2a`.

Applying the code above, the UUID is read from `value[3..18]` little-endian:
`00 2a 04 00 02 05 00 01 2a 06 00 02 07 00 04 2a` reversed →
`2a040007-0200-062a-0100-050200042a00` — **exactly the malformed UUID in the
report.** BlueZ did precisely what the bytes (mislabelled as one 21-byte record)
told it to.

### 3. The same bytes are actually three valid 7-byte (16-bit UUID) records

Regrouping the record as 7-byte, 16-bit-UUID characteristic declarations
(`handle(2) | props(1) | value-handle(2) | uuid16(2)`):

| Bytes | Decl. handle | Props | Value handle | UUID |
|---|---|---|---|---|
| `02 00 02 03 00 00 2a` | 0x0002 | 0x02 | 0x0003 | **0x2A00** (Device Name) |
| `04 00 02 05 00 01 2a` | 0x0004 | 0x02 | 0x0005 | **0x2A01** (Appearance) |
| `06 00 02 07 00 04 2a` | 0x0006 | 0x02 | 0x0007 | **0x2A04** (Peripheral Preferred Connection Parameters) |

These are the three standard GAP-service characteristics. The wire data is three
legitimate 7-byte records, but the device set the response's **Length field to
21 instead of 7**, so BlueZ (and btmon) collapse them into one 128-bit record
with a garbage UUID, and the real characteristics are lost.

### 4. Why `gatttool` works

The report shows BlueZ negotiating a large ATT MTU (client 517 / server 160),
whereas `gatttool` uses the default 23-byte MTU. The device's framing of the
`Read By Type Response` is MTU-dependent; at the small MTU it returns correctly
labelled 7-byte records. This points to the defect being in the device firmware,
not BlueZ.

### 5. Current BlueZ would behave identically

`bt_gatt_iter_next_characteristic()` is unchanged on `master`. Commits to
`src/shared/gatt-client.c` since 5.82 are unrelated (`DB_OUT_OF_SYNC` caching
`7c9c8630c`, a notify-data leak `a2ef82f1a`, `read_long` `be36a9c9d`, typos).
There is no BlueZ-side fix to wait for because there is no BlueZ-side bug.

### Conclusion and recommended next steps

- This is **not** a BlueZ defect; do not file it upstream against BlueZ.
- Confirm definitively (read-only): re-capture and read the raw **Length byte**
  of that `Read By Type Response` (expect `0x15` = 21 while the records are
  7-byte); and re-test with a forced small ATT MTU — if the device then returns
  `data_len = 7` and BlueZ exposes `fff1`/`fff2`, the MTU-dependent device
  framing is proven.
- Practical mitigations live on our side: constrain the ATT MTU for this device,
  special-case it, or use a `gatttool`-style ATT discovery path for it.

## Impact

Applications using the BlueZ D-Bus API (including the `bleak` Python library)
cannot communicate with this device, even though the underlying BLE connection
and GATT discovery work correctly at the HCI level. Because the root cause is the
peer device's malformed response, the same failure is expected from any host
stack that trusts the ATT length field; the fix must be a device-specific
workaround (e.g. MTU constraint), not a stack change.




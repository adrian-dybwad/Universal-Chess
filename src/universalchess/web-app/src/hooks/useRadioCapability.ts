import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';

/**
 * Which radios the board physically has.
 *
 * Some supported boards have none: a plain Raspberry Pi Zero (no "W") has no
 * wireless die, so its Wi-Fi and Bluetooth controls could never do anything.
 * The board process hides the same two entries from its own Connectivity menu
 * from the same signal (see `board/wireless_capability.py`), so the two surfaces
 * agree.
 */
export interface RadioCapability {
  hasWifi: boolean;
  hasBluetooth: boolean;
  /**
   * Whether the probe has finished, successfully or not.
   *
   * Callers must render (and poll) nothing radio-related while this is false.
   * Acting on the assumption below before the answer arrives would flash the
   * controls onto an unequipped board and spend a status request on hardware
   * that is not fitted, which is precisely what the gate exists to avoid.
   */
  probed: boolean;
}

/**
 * Assumed once the probe has finished but could not be read.
 *
 * Fails *open*, matching the menu engine's rule for an unreadable gate: a
 * transient 502 must never take Wi-Fi setup away from a board that has Wi-Fi,
 * which is the one failure the user could not recover from through the UI.
 * Showing an inert card on an unequipped board while the probe is unreachable is
 * the cheaper mistake.
 */
const ASSUME_PRESENT = { hasWifi: true, hasBluetooth: true } as const;

/**
 * Read the board's radio capability, or `null` when the answer is unusable.
 *
 * Both fields must arrive as booleans to count: a payload from an older board
 * build reports neither, and reading a missing field as `false` would hide
 * working controls on every board running that build.
 */
async function readCapability(): Promise<Omit<RadioCapability, 'probed'> | null> {
  try {
    const response = await apiFetch('/api/system/info');
    if (!response.ok) return null;
    const data = await response.json();
    if (typeof data?.has_wifi === 'boolean' && typeof data?.has_bluetooth === 'boolean') {
      return { hasWifi: data.has_wifi, hasBluetooth: data.has_bluetooth };
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Read the board's radio capability once per mount.
 *
 * Deliberately not memoized across components: the value is one small read, and
 * a module-level cache would have to be invalidated for a USB dongle attached
 * while the page is open (and would leak between tests).
 */
export function useRadioCapability(): RadioCapability {
  const [capability, setCapability] = useState<RadioCapability>({
    ...ASSUME_PRESENT,
    probed: false,
  });

  useEffect(() => {
    let active = true;
    const read = async () => {
      const detected = await readCapability();
      if (active) setCapability({ ...(detected ?? ASSUME_PRESENT), probed: true });
    };
    void read();
    return () => {
      active = false;
    };
  }, []);

  return capability;
}

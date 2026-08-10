/**
 * Which online account a player slot plays as.
 *
 * The web mirror of the board's ``account_store``. Kept as pure functions in
 * their own module so the two platforms can be compared rule for rule, and so
 * the rules are readable without the Settings page around them.
 */
import type { AccountRecord } from '../types/accounts';

/**
 * Resolve the concrete account id a player slot binds to for an online type.
 *
 * Mirror of the board's ``account_store.resolve_account_id`` so the two
 * platforms judge the same effective account: an explicit, still-existing id
 * resolves to itself; an empty id, or one whose account is gone, falls back to
 * the default (first) account. ``null`` when the type has no accounts. The web
 * account list arrives already sorted by id (like the board), so index 0 is the
 * same "default" both platforms use.
 */
export function resolveAccountId(accountsOfType: AccountRecord[], accountId: string): string | null {
  if (accountId && accountsOfType.some((a) => a.id === accountId)) return accountId;
  return accountsOfType[0]?.id ?? null;
}

/** Accounts a slot may bind after excluding the account the other slot uses. */
export interface SlotAccountChoices {
  defaultAllowed: boolean;
  accounts: AccountRecord[];
}

/**
 * Accounts this slot may bind, excluding the one the other slot uses -- the web
 * mirror of ``account_store.selectable_accounts_for_slot``. One online account
 * may not play both sides, so the account the other slot resolves to is removed
 * and the "Default account" option is withheld when Default would resolve to
 * that same account. ``sameType`` is whether the other slot is the same online
 * type (only then can they share an account space).
 */
export function selectableAccountsForSlot(
  accountsOfType: AccountRecord[],
  sameType: boolean,
  otherAccount: string,
): SlotAccountChoices {
  const taken = sameType ? resolveAccountId(accountsOfType, otherAccount) : null;
  const defaultId = accountsOfType[0]?.id ?? null;
  return {
    defaultAllowed: taken === null || defaultId !== taken,
    accounts: accountsOfType.filter((a) => a.id !== taken),
  };
}

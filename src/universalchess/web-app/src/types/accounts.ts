/**
 * One saved online account as returned by GET /api/accounts. Secrets are never
 * sent in cleartext: each secret field is reported only as a boolean in
 * `secretsSet` (e.g. `{ api_token: true }`).
 */
export interface AccountRecord {
  type: string;
  id: string;
  identity: string;
  values: Record<string, string>;
  secretsSet: Record<string, boolean>;
  /** Lichess host id (org / dev). Absent for other providers. */
  host?: string;
  /** Chooser text, e.g. lichess.org:Alice. */
  label?: string;
}

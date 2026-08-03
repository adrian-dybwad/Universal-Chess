/**
 * Hash-pinned loader for the opt-in deep-analysis engine.
 *
 * The appliance ships no chess engine to the browser. When the user turns on
 * `game.deep_analysis`, the review page fetches Stockfish 16 NNUE from jsDelivr
 * and runs it locally. This is the only code path in the product that reaches a
 * third party.
 *
 * Security model
 * --------------
 * The Content-Security-Policy grants jsDelivr `connect-src` only, never
 * `script-src`, so the CDN cannot serve executable script directly. Every asset
 * is fetched here, on the main thread, and its SHA-256 compared against a pin
 * recorded below before anything is built from it. Only then is the worker
 * created from a Blob of the verified source. A mismatch throws and nothing is
 * constructed -- verifying after creating the worker would be no verification at
 * all, since the source has executed by then.
 *
 * Engine specifics (established against stockfish@16.0.0, not assumed)
 * --------------------------------------------------------------------
 * - The single-threaded build needs no SharedArrayBuffer, so no COOP/COEP.
 * - It locates its .wasm from the worker URL's fragment, which is why the
 *   worker is created as `<jsBlobUrl>#<encoded wasmBlobUrl>`.
 * - It defaults to *classical* evaluation and loads no net. `EvalFile` accepts
 *   an arbitrary path, so the verified net is handed over as its own object URL
 *   and `Use NNUE` switched on; without both, the 39 MB download buys nothing.
 * - Once running it speaks plain UCI text over the worker message port.
 *
 * Licence: the engine is GPL-3.0 (see Licenses page). It is not conveyed by this
 * project -- the browser is directed to fetch it -- but its terms are recorded.
 */

/** Origin the assets are pinned to. Mirrors DEEP_ANALYSIS_CDN_ORIGIN in web/app.py. */
export const DEEP_ANALYSIS_CDN_ORIGIN = 'https://cdn.jsdelivr.net';

const PACKAGE_BASE = `${DEEP_ANALYSIS_CDN_ORIGIN}/npm/stockfish@16.0.0/src`;

/** One pinned CDN asset. */
export interface PinnedAsset {
  url: string;
  /** Lowercase hex SHA-256 of the exact bytes expected at `url`. */
  sha256: string;
  /** Expected size in bytes; used only to report the download cost. */
  bytes: number;
}

/**
 * The three assets, in load order, pinned to an immutable package version.
 *
 * Hashes were taken from the served responses. The net's filename embeds its own
 * hash by Stockfish convention, so the engine independently rejects a net that
 * is not the one it expects -- but that check happens after the bytes have been
 * parsed, so it does not replace the pin here.
 */
export const DEEP_ANALYSIS_ASSETS: readonly PinnedAsset[] = [
  {
    url: `${PACKAGE_BASE}/stockfish-nnue-16-single.js`,
    sha256: 'e2958bb89fc6ee0faedde87284bbb7e14da2ab224f06ca1bd82e62eaca87d00b',
    bytes: 25594,
  },
  {
    url: `${PACKAGE_BASE}/stockfish-nnue-16-single.wasm`,
    sha256: 'a7acf7f20cb81d755b39b3dd42a4bdfd6e8c8d3d203d9fbdc525e40e1f68df08',
    bytes: 575029,
  },
  {
    url: `${PACKAGE_BASE}/nn-5af11540bbfe.nnue`,
    sha256: '5af11540bbfefcb54e38c5dd000cab4b469dfa7599a1d55be5d2722c20a8929b',
    bytes: 40119326,
  },
];

/** Total first-use download, in whole megabytes, for the settings warning. */
export const DEEP_ANALYSIS_DOWNLOAD_MB = Math.round(
  DEEP_ANALYSIS_ASSETS.reduce((total, asset) => total + asset.bytes, 0) / 1_000_000,
);

/** Side effects the loader needs, injected so the logic is testable. */
export interface DeepAnalysisDeps {
  assets?: readonly PinnedAsset[];
  fetchImpl?: typeof fetch;
  digest?: (bytes: Uint8Array) => Promise<ArrayBuffer>;
  createObjectURL?: (blob: Blob) => string;
  createWorker?: (url: string) => Worker;
}

function resolveDeps(deps: DeepAnalysisDeps) {
  return {
    assets: deps.assets ?? DEEP_ANALYSIS_ASSETS,
    fetchImpl: deps.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args)),
    digest:
      deps.digest ??
      ((bytes: Uint8Array) =>
        crypto.subtle.digest('SHA-256', bytes as unknown as ArrayBuffer)),
    createObjectURL: deps.createObjectURL ?? ((blob: Blob) => URL.createObjectURL(blob)),
    createWorker: deps.createWorker ?? ((url: string) => new Worker(url)),
  };
}

function toHex(digest: ArrayBuffer): string {
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

/**
 * Fetch one asset and return its bytes only if they match the pin.
 *
 * @throws if the request fails or the digest differs. Callers must not fall
 *   back to the unverified bytes: an attacker who can substitute the response
 *   is exactly the case this guards.
 */
export async function fetchVerifiedAsset(
  asset: PinnedAsset,
  deps: DeepAnalysisDeps = {},
): Promise<Uint8Array> {
  const { fetchImpl, digest } = resolveDeps(deps);

  const response = await fetchImpl(asset.url);
  if (!response.ok) {
    throw new Error(`Deep analysis asset ${asset.url} failed to download (${response.status})`);
  }

  const bytes = new Uint8Array(await response.arrayBuffer());
  const actual = toHex(await digest(bytes));
  if (actual !== asset.sha256) {
    throw new Error(
      `Deep analysis asset ${asset.url} failed its checksum ` +
        `(expected ${asset.sha256}, got ${actual})`,
    );
  }
  return bytes;
}

/** A loaded deep-analysis engine, already configured for NNUE evaluation. */
export interface DeepAnalysisEngine {
  /** Speaks plain UCI text; the caller owns termination. */
  worker: Worker;
  /** Object URLs to revoke once the worker is terminated. */
  objectUrls: readonly string[];
}

/**
 * Fetch, verify and start the deep-analysis engine.
 *
 * All three assets are verified before any of them is turned into an object URL
 * or a worker, so a failure anywhere leaves nothing constructed and nothing
 * executed.
 */
export async function loadDeepAnalysisEngine(
  deps: DeepAnalysisDeps = {},
): Promise<DeepAnalysisEngine> {
  const { assets, createObjectURL, createWorker } = resolveDeps(deps);
  const [engineSource, wasmBytes, netBytes] = await Promise.all(
    assets.map((asset) => fetchVerifiedAsset(asset, deps)),
  );

  const jsUrl = createObjectURL(
    new Blob([engineSource as BlobPart], { type: 'text/javascript' }),
  );
  const wasmUrl = createObjectURL(
    new Blob([wasmBytes as BlobPart], { type: 'application/wasm' }),
  );
  const netUrl = createObjectURL(
    new Blob([netBytes as BlobPart], { type: 'application/octet-stream' }),
  );

  const worker = createWorker(`${jsUrl}#${encodeURIComponent(wasmUrl)}`);
  worker.postMessage('uci');
  worker.postMessage(`setoption name EvalFile value ${netUrl}`);
  worker.postMessage('setoption name Use NNUE value true');
  worker.postMessage('isready');

  return { worker, objectUrls: [jsUrl, wasmUrl, netUrl] };
}

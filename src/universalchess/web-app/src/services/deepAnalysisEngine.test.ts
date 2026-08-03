// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import {
  DEEP_ANALYSIS_ASSETS,
  DEEP_ANALYSIS_CDN_ORIGIN,
  fetchVerifiedAsset,
  loadDeepAnalysisEngine,
} from './deepAnalysisEngine';

/**
 * Guards the hash-pinned CDN loader.
 *
 * This is the only code path in the product that reaches a third party, and it
 * ends in executing whatever comes back. The Content-Security-Policy
 * deliberately does NOT list the CDN under script-src, so the SHA-256 pin is the
 * single control standing between a substituted CDN response and arbitrary code
 * running in the user's browser.
 *
 * How a regression manifests
 * --------------------------
 * A verification that logs instead of throws, that compares nothing on an empty
 * digest, or that builds the worker before awaiting the check, all produce the
 * same outcome: unverified bytes execute. Each test below therefore asserts on
 * the *absence* of execution, not just on the rejection.
 */

const encoder = new TextEncoder();

/** Real SHA-256 of `bytes` as lowercase hex, via the platform WebCrypto. */
async function realDigest(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as unknown as ArrayBuffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

/** A fetch stand-in returning fixed bytes per URL; unknown URLs 404. */
function fakeFetch(byUrl: Record<string, Uint8Array>) {
  return vi.fn(async (url: string) => {
    const bytes = byUrl[url];
    if (!bytes) return { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
    return {
      ok: true,
      status: 200,
      arrayBuffer: async () => bytes.slice().buffer,
    };
  });
}

describe('deep analysis asset pinning', () => {
  it('pins all three assets to one immutable CDN version', async () => {
    // A floating version (or a second origin) would let the bytes change under a
    // fixed hash, which can only ever fail closed -- but it also means the pins
    // silently stop describing what is served. Everything must come from one
    // exact package version on the one origin the CSP permits.
    const urls = DEEP_ANALYSIS_ASSETS.map((a) => a.url);
    expect(urls).toHaveLength(3);
    for (const url of urls) {
      expect(url.startsWith(`${DEEP_ANALYSIS_CDN_ORIGIN}/npm/stockfish@16.0.0/`)).toBe(true);
    }
    // Distinct assets, each with a full 64-hex-character SHA-256.
    expect(new Set(urls).size).toBe(3);
    for (const asset of DEEP_ANALYSIS_ASSETS) {
      expect(asset.sha256).toMatch(/^[0-9a-f]{64}$/);
    }
  });
});

describe('fetchVerifiedAsset', () => {
  it('returns the bytes when the digest matches the pin', async () => {
    // The happy path must actually verify rather than short-circuit: the pin
    // used here is computed from the same bytes with the platform digest, so a
    // comparison that always passed and one that works look different only in
    // the mismatch test below.
    const bytes = encoder.encode('engine source');
    const sha256 = await realDigest(bytes);

    const result = await fetchVerifiedAsset(
      { url: 'https://cdn.example/x.js', sha256, bytes: bytes.length },
      { fetchImpl: fakeFetch({ 'https://cdn.example/x.js': bytes }) as never },
    );

    expect(Array.from(result)).toEqual(Array.from(bytes));
  });

  it('rejects when the bytes do not match the pin', async () => {
    // The core control. Regression: a mismatch that resolves anyway hands
    // substituted CDN content straight to the Blob worker.
    const expected = await realDigest(encoder.encode('engine source'));
    const tampered = encoder.encode('engine source with a backdoor');

    await expect(
      fetchVerifiedAsset(
        { url: 'https://cdn.example/x.js', sha256: expected, bytes: tampered.length },
        { fetchImpl: fakeFetch({ 'https://cdn.example/x.js': tampered }) as never },
      ),
    ).rejects.toThrow(/checksum/i);
  });

  it('rejects a failed request instead of verifying empty bytes', async () => {
    // A 404 body is zero bytes, which has a perfectly valid digest of its own.
    // Regression: skipping the status check turns every CDN outage into a
    // confusing checksum error, or worse, hashes an error page as if it were
    // the engine.
    await expect(
      fetchVerifiedAsset(
        { url: 'https://cdn.example/missing.js', sha256: 'a'.repeat(64), bytes: 1 },
        { fetchImpl: fakeFetch({}) as never },
      ),
    ).rejects.toThrow(/404/);
  });
});

describe('loadDeepAnalysisEngine', () => {
  /** Deps wired to serve `overrides` (or correct bytes) for the pinned URLs. */
  async function depsFor(overrides: Record<string, Uint8Array> = {}) {
    const byUrl: Record<string, Uint8Array> = {};
    const assets: Array<{ url: string; sha256: string; bytes: number }> = [];
    for (const [index, asset] of DEEP_ANALYSIS_ASSETS.entries()) {
      const content = encoder.encode(`asset-${index}-content`);
      byUrl[asset.url] = overrides[asset.url] ?? content;
      assets.push({ url: asset.url, sha256: await realDigest(content), bytes: content.length });
    }
    const objectUrls: Blob[] = [];
    const workers: Array<{ url: string; posted: string[]; worker: Worker }> = [];
    return {
      byUrl,
      objectUrls,
      workers,
      deps: {
        assets,
        fetchImpl: fakeFetch(byUrl) as never,
        createObjectURL: (blob: Blob) => {
          objectUrls.push(blob);
          return `blob:mock/${objectUrls.length - 1}`;
        },
        createWorker: (url: string) => {
          const posted: string[] = [];
          const worker = { postMessage: (m: string) => posted.push(m) } as unknown as Worker;
          workers.push({ url, posted, worker });
          return worker;
        },
      },
    };
  }

  it('builds the worker from the verified engine source with the wasm in the URL hash', async () => {
    // The engine locates its .wasm from the worker URL's fragment; without it
    // the worker resolves a bare filename against an opaque blob: base and the
    // engine never starts. The worker source must be the bytes that were
    // verified, not a URL the CDN could answer differently a second time.
    const { deps, objectUrls, workers } = await depsFor();

    const engine = await loadDeepAnalysisEngine(deps);

    expect(workers).toHaveLength(1);
    const [jsUrl, hash] = workers[0].url.split('#');
    // Blob 0 is the engine source, blob 1 the wasm, blob 2 the net.
    expect(jsUrl).toBe('blob:mock/0');
    expect(decodeURIComponent(hash)).toBe('blob:mock/1');
    expect(await objectUrls[0].text()).toBe('asset-0-content');
    expect(engine.worker).toBe(workers[0].worker);
  });

  it('points the engine at the verified net and turns NNUE on', async () => {
    // This build defaults to classical evaluation and loads no net: without
    // both options the user opts into a 39 MB download and still gets the
    // weaker evaluation. EvalFile must be the verified net's own object URL.
    const { deps, workers } = await depsFor();

    await loadDeepAnalysisEngine(deps);

    expect(workers[0].posted).toContain('setoption name EvalFile value blob:mock/2');
    expect(workers[0].posted).toContain('setoption name Use NNUE value true');
  });

  it('creates no worker when any asset fails verification', async () => {
    // Verification is worthless if the worker is created first and torn down
    // after: by then the source has already executed. Assert nothing was built
    // at all, not merely that the promise rejected.
    const tampered = encoder.encode('tampered');
    const { deps, workers, objectUrls } = await depsFor({
      [DEEP_ANALYSIS_ASSETS[0].url]: tampered,
    });

    await expect(loadDeepAnalysisEngine(deps)).rejects.toThrow(/checksum/i);

    expect(workers).toHaveLength(0);
    expect(objectUrls).toHaveLength(0);
  });

  it('creates no worker when the net alone fails verification', async () => {
    // The net is 98% of the download and the last asset checked, so it is the
    // one most likely to be truncated by a proxy. It is also parsed by the
    // engine, so a corrupt net must not reach it -- and since it is verified
    // before anything is built, no worker exists either.
    const { deps, workers, objectUrls } = await depsFor({
      [DEEP_ANALYSIS_ASSETS[2].url]: encoder.encode('truncated net'),
    });

    await expect(loadDeepAnalysisEngine(deps)).rejects.toThrow(/checksum/i);

    expect(workers).toHaveLength(0);
    expect(objectUrls).toHaveLength(0);
  });
});

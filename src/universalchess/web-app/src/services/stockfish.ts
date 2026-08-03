import type { AnalysisResult } from '../types/game';
import { loadDeepAnalysisEngine, type DeepAnalysisDeps } from './deepAnalysisEngine';

// Upper bound on how long a single load + UCI handshake may take before it is
// treated as failed. Generous because the first load downloads roughly 39 MB
// from the CDN and then compiles the WebAssembly, which on a cold cache or a
// slow device takes well over a minute. A too-tight bound rejects an engine that
// would have been ready moments later.
const INIT_TIMEOUT_MS = 180000;

interface QueuedRequest {
  fen: string;
  depth: number;
  resolve: (result: AnalysisResult) => void;
  reject: (error: Error) => void;
}

/**
 * Wrapper around the opt-in deep-analysis engine.
 *
 * Nothing is bundled: the engine is fetched from a CDN and hash-verified by
 * {@link loadDeepAnalysisEngine}, which only happens once the user turns on the
 * server-side `game.deep_analysis` setting (the CSP blocks the fetch otherwise).
 * Board-sourced evaluations, which every install gets, do not come through here
 * -- they arrive with the game state.
 *
 * Handles request queuing internally: multiple analyze() calls are safe and are
 * processed sequentially in FIFO order.
 */
export class StockfishService {
  private worker: Worker | null = null;
  private objectUrls: readonly string[] = [];
  private isReady = false;
  private initPromise: Promise<void> | null = null;
  private loaderDeps: DeepAnalysisDeps;

  // Request queue
  private queue: QueuedRequest[] = [];
  private currentRequest: QueuedRequest | null = null;
  private currentResult: Partial<AnalysisResult> = {};

  /**
   * @param loaderDeps Side effects for the CDN loader. Defaults to the real
   *   fetch/WebCrypto/Worker; overridden in tests.
   */
  constructor(loaderDeps: DeepAnalysisDeps = {}) {
    this.loaderDeps = loaderDeps;
  }

  async init(): Promise<void> {
    if (this.initPromise) {
      return this.initPromise;
    }

    if (this.worker && this.isReady) {
      return Promise.resolve();
    }

    this.initPromise = this.doInit();
    return this.initPromise;
  }

  private async doInit(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      // A single init attempt settles exactly once. `settled` guards against the
      // timeout, onerror, and readiness paths racing each other (e.g. uciok
      // arriving just as the timeout fires).
      let settled = false;
      let checkReady: ReturnType<typeof setInterval> | undefined;

      const clearTimers = () => {
        if (checkReady !== undefined) clearInterval(checkReady);
        clearTimeout(timeout);
      };

      const fail = (error: Error) => {
        if (settled) return;
        settled = true;
        clearTimers();
        // Tear the partially-initialized worker down so the next init() starts
        // from a clean slate. Leaving it alive leaks the worker and lets a late
        // uciok flip isReady to true behind a rejected promise, an inconsistent
        // state where callers think init failed but the service reports ready.
        this.releaseWorker();
        this.initPromise = null;
        reject(error);
      };

      const succeed = () => {
        if (settled) return;
        settled = true;
        clearTimers();
        console.log('[Stockfish] Ready');
        resolve();
      };

      // The timer starts before the download so a stalled fetch is bounded too,
      // not just the WebAssembly compile.
      const timeout = setTimeout(() => {
        console.error('[Stockfish] Init timeout');
        fail(new Error('Stockfish init timeout'));
      }, INIT_TIMEOUT_MS);

      loadDeepAnalysisEngine(this.loaderDeps)
        .then(({ worker, objectUrls }) => {
          // A load that finishes after the timeout already failed must not
          // install itself behind a rejected promise.
          if (settled) {
            worker.terminate();
            for (const url of objectUrls) URL.revokeObjectURL(url);
            return;
          }
          this.worker = worker;
          this.objectUrls = objectUrls;
          worker.onmessage = (e) => this.handleMessage(e.data);
          worker.onerror = (e) => {
            console.error('[Stockfish] Worker error:', e);
            fail(new Error(`Deep analysis engine failed to start: ${e.message}`));
          };
          checkReady = setInterval(() => {
            if (this.isReady) {
              succeed();
            }
          }, 100);
        })
        .catch((e) => {
          console.error('[Stockfish] Init failed:', e);
          fail(e instanceof Error ? e : new Error(String(e)));
        });
    });
  }

  /** Terminate the worker and release the object URLs holding ~39 MB. */
  private releaseWorker(): void {
    this.worker?.terminate();
    this.worker = null;
    for (const url of this.objectUrls) {
      URL.revokeObjectURL(url);
    }
    this.objectUrls = [];
    this.isReady = false;
  }

  private handleMessage(line: string): void {
    if (line === 'uciok') {
      this.isReady = true;
      this.worker?.postMessage('isready');
      return;
    }

    if (line === 'readyok') {
      return;
    }

    // Parse score from info lines
    if (line.startsWith('info') && line.includes('score')) {
      const cpMatch = line.match(/score cp (-?\d+)/);
      const mateMatch = line.match(/score mate (-?\d+)/);
      const depthMatch = line.match(/depth (\d+)/);

      if (cpMatch) {
        this.currentResult.score = parseInt(cpMatch[1], 10);
        this.currentResult.mate = null;
      }
      if (mateMatch) {
        this.currentResult.mate = parseInt(mateMatch[1], 10);
        this.currentResult.score = null;
      }
      if (depthMatch) {
        this.currentResult.depth = parseInt(depthMatch[1], 10);
      }
    }

    // Parse bestmove - analysis complete
    if (line.startsWith('bestmove')) {
      const match = line.match(/bestmove (\S+)/);
      if (match) {
        this.currentResult.bestMove = match[1];
      }

      // Resolve current request
      if (this.currentRequest) {
        this.currentRequest.resolve({
          fen: this.currentRequest.fen,
          score: this.currentResult.score ?? null,
          mate: this.currentResult.mate ?? null,
          bestMove: this.currentResult.bestMove ?? null,
          depth: this.currentResult.depth ?? 0,
        });
        this.currentRequest = null;
      }

      // Process next in queue
      this.processNext();
    }
  }

  private processNext(): void {
    if (this.currentRequest) return;  // Already processing
    if (this.queue.length === 0) return;  // Nothing to process
    if (!this.worker || !this.isReady) return;  // Not ready

    this.currentRequest = this.queue.shift()!;
    this.currentResult = {};

    this.worker.postMessage(`position fen ${this.currentRequest.fen}`);
    this.worker.postMessage(`go depth ${this.currentRequest.depth}`);
  }

  /**
   * Analyze a position. Requests are queued and processed sequentially.
   *
   * @param priority When true, the request jumps to the front of the queue so
   *   it runs next (after any in-flight request completes). Used for the
   *   position the user is actually viewing, so its eval and best move surface
   *   promptly instead of waiting behind a large background-fill backlog.
   */
  async analyze(fen: string, depth = 18, priority = false): Promise<AnalysisResult> {
    if (!this.isReady) {
      await this.init();
    }

    if (!this.worker) {
      throw new Error('Stockfish worker not available');
    }

    return new Promise((resolve, reject) => {
      const request = { fen, depth, resolve, reject };
      if (priority) {
        this.queue.unshift(request);
      } else {
        this.queue.push(request);
      }
      this.processNext();
    });
  }

  get ready(): boolean {
    return this.isReady;
  }

  stop(): void {
    // Clear queue
    for (const req of this.queue) {
      req.reject(new Error('Analysis stopped'));
    }
    this.queue = [];
    
    // Stop current analysis
    if (this.currentRequest) {
      this.currentRequest.reject(new Error('Analysis stopped'));
      this.currentRequest = null;
    }
    
    this.worker?.postMessage('stop');
  }

  destroy(): void {
    this.stop();
    this.releaseWorker();
    this.initPromise = null;
  }
}

// Singleton instance
let instance: StockfishService | null = null;

export function getStockfishService(): StockfishService {
  if (!instance) {
    instance = new StockfishService();
  }
  return instance;
}

/**
 * Release the singleton's worker and the object URLs holding the ~39 MB engine.
 *
 * A no-op when no engine was ever loaded, so callers reacting to the deep
 * analysis setting being off do not construct the very service they are trying
 * to avoid.
 */
export function destroyStockfishService(): void {
  instance?.destroy();
  instance = null;
}

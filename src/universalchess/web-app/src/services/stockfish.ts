import type { AnalysisResult } from '../types/game';

// Upper bound on how long a single worker load + UCI handshake may take before
// it is treated as failed. Sized generously because the first load compiles the
// Stockfish WASM, which on a cold cache / slow device can take well over ten
// seconds. A too-tight bound here rejects a worker that would have been ready
// moments later, so keep this comfortably above observed cold-start times.
const INIT_TIMEOUT_MS = 30000;

interface QueuedRequest {
  fen: string;
  depth: number;
  resolve: (result: AnalysisResult) => void;
  reject: (error: Error) => void;
}

/**
 * Stockfish web worker wrapper for chess analysis.
 * 
 * Handles request queuing internally - multiple analyze() calls are safe.
 * Requests are processed sequentially in FIFO order.
 */
export class StockfishService {
  private worker: Worker | null = null;
  private isReady = false;
  private initPromise: Promise<void> | null = null;
  private workerPath: string;
  
  // Request queue
  private queue: QueuedRequest[] = [];
  private currentRequest: QueuedRequest | null = null;
  private currentResult: Partial<AnalysisResult> = {};

  constructor(workerPath = '/stockfish/stockfish.js') {
    this.workerPath = workerPath;
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
    return new Promise((resolve, reject) => {
      // A single init attempt settles exactly once. `settled` guards against the
      // timeout, onerror, and readiness paths racing each other (e.g. uciok
      // arriving just as the timeout fires).
      let settled = false;
      let checkReady: ReturnType<typeof setInterval> | undefined;
      let timeout: ReturnType<typeof setTimeout> | undefined;

      const clearTimers = () => {
        if (checkReady !== undefined) clearInterval(checkReady);
        if (timeout !== undefined) clearTimeout(timeout);
      };

      const fail = (error: Error) => {
        if (settled) return;
        settled = true;
        clearTimers();
        // Tear the partially-initialized worker down so the next init() starts
        // from a clean slate. Leaving it alive leaks the worker and lets a late
        // uciok flip isReady to true behind a rejected promise, an inconsistent
        // state where callers think init failed but the service reports ready.
        this.worker?.terminate();
        this.worker = null;
        this.isReady = false;
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

      try {
        console.log(`[Stockfish] Loading worker from: ${this.workerPath}`);
        this.worker = new Worker(this.workerPath);

        this.worker.onmessage = (e) => this.handleMessage(e.data);

        this.worker.onerror = (e) => {
          console.error('[Stockfish] Worker error:', e);
          fail(new Error(`Stockfish worker failed to load: ${e.message}`));
        };

        this.worker.postMessage('uci');

        timeout = setTimeout(() => {
          console.error('[Stockfish] Init timeout');
          fail(new Error('Stockfish init timeout'));
        }, INIT_TIMEOUT_MS);

        checkReady = setInterval(() => {
          if (this.isReady) {
            succeed();
          }
        }, 100);
      } catch (e) {
        console.error('[Stockfish] Init failed:', e);
        fail(e instanceof Error ? e : new Error(String(e)));
      }
    });
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
    this.worker?.terminate();
    this.worker = null;
    this.isReady = false;
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

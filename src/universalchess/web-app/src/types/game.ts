/**
 * Game state received from SSE /events endpoint.
 * Property names match the snake_case JSON from Python backend.
 */
export interface GameState {
  fen: string;
  fen_full: string;
  pgn: string;
  move_number: number;
  turn: 'w' | 'b';
  white: string;
  black: string;
  result: string | null;
  /** How the game ended: 'checkmate', 'stalemate', 'resignation', 'time_forfeit', etc. */
  termination: string | null;
  game_id: number | null;
  game_over: boolean;
  /** Last move executed in UCI format (e.g., 'e2e4') */
  last_move: string | null;
  /** Move pending on the physical board (engine/Lichess move waiting to be executed) */
  pending_move: string | null;
  /**
   * Whether the current game is Chess960 (Fischer Random). Reports which
   * castling rules the position uses; absent/false for a standard game.
   */
  chess960?: boolean;
  /** The game's starting FEN (generated 960 start, or the standard start). */
  start_fen?: string | null;
  /**
   * Authoritative per-ply positions (python-chess computed), start first. The
   * web builds and navigates the move list from these for both variants instead
   * of replaying the PGN in the browser (chess.js is no longer used).
   */
  positions?: PositionEntry[] | null;
  /**
   * Active in-play warning for the latest position, mirroring the e-paper alert:
   * 'check' (the side to move is in check) or 'queen' (the mover's queen is under
   * attack). Absent/null when neither applies; suppressed once the game is over
   * (a checkmate reads as game over, not as a check warning).
   */
  alert?: GameAlert | null;
  /**
   * Algebraic square the alert refers to (e.g. 'e8'): the checked king for a
   * 'check' alert, or the threatened queen for a 'queen' alert. Null when there
   * is no alert. Lets the board highlight the piece at risk.
   */
  alert_square?: string | null;
}

/** The closed set of in-play warnings the board broadcasts; see GameState.alert. */
export type GameAlert = 'check' | 'queen';

/**
 * One authoritative position in a game's history, computed server-side by
 * python-chess so it is correct for both standard and Chess960 castling.
 */
export interface PositionEntry {
  /** Full FEN of the position (placement + turn/castling/etc.). */
  fen: string;
  /** SAN of the move that produced this position; null for the start. */
  san: string | null;
  /** UCI of the move that produced this position; null for the start. */
  uci: string | null;
}

/**
 * Stockfish analysis result for a position.
 */
export interface AnalysisResult {
  fen: string;
  score: number | null;
  mate: number | null;
  bestMove: string | null;
  depth: number;
}

/**
 * Game record from database.
 */
export interface GameRecord {
  id: number;
  white: string | null;
  black: string | null;
  result: string | null;
  /**
   * Lifecycle status derived server-side from `result`: 'in_progress' (NULL
   * result), 'abandoned' ('*' result), or 'finished'. The Games screen offers
   * Resume only for in-progress and abandoned games.
   */
  status?: 'in_progress' | 'abandoned' | 'finished';
  created_at: string;
  source: string | null;
}

/**
 * Which step of bringing an engine up failed.
 *
 * The phase selects both the sentence shown and the action offered: an engine
 * that never installed needs Install, while one that installed and will not
 * start needs a rebuild or reinstall.
 */
export type EngineFailurePhase = 'install' | 'initialize';

/**
 * Why an engine is unusable, as recorded by the backend.
 *
 * `reason_code` and `detail` are fixed tokens, never text derived from an
 * exception: this payload is served by an endpoint that is not auth-gated, and
 * the underlying exception messages contain engine filesystem paths. The fuller
 * text lives in the auth-gated system event log.
 */
export interface EngineFailure {
  phase: EngineFailurePhase;
  /** Stable token naming the failure mode; localized for display. */
  reason_code: string;
  /** Short technical token such as "OSError ENOEXEC", or null. */
  detail: string | null;
  /** Unix seconds when the failure was recorded. */
  failed_at: number | null;
  /**
   * Whether the user acknowledged this particular failure. Hides the notice
   * only -- the engine is still unusable, so the badge is unaffected. A later
   * failure on the same engine arrives undismissed.
   */
  dismissed: boolean;
}

/**
 * Engine definition from backend.
 */
export interface EngineDefinition {
  name: string;
  display_name: string;
  description: string;
  summary: string;
  /**
   * Page describing the engine (usually its repository), resolved server-side
   * from the catalog and rendered as the card's "learn more" link. Empty for an
   * engine with no such page: the bundled novelty engines, which exist only
   * inside this project, and operator-added custom engines.
   */
  info_url: string;
  installed: boolean;
  has_prebuilt: boolean;
  /**
   * How long a fresh install is expected to take, in minutes, from the catalog.
   * 0 means there is nothing to wait for: a system package, or a bundled engine
   * whose install just writes a launcher shim.
   */
  estimated_install_minutes: number;
  /**
   * Whether this engine exposes a user-editable UCI option schema (served by
   * GET /api/engines/{name}/profiles). Drives whether the "Configure profiles"
   * action is shown. True for any installed engine whose binary can be probed,
   * including the Stockfish system package -- not limited to a curated list.
   */
  has_profiles: boolean;
  /**
   * Whether the engine actually produced a strength ladder, read server-side
   * from the seeded .uci on disk without launching anything.
   *
   * Distinct from `installed`, which only says a binary file exists and stays
   * true forever once written. An engine can install cleanly and then fail to
   * start -- a build for the wrong architecture, say -- leaving it "installed"
   * with no profiles, no Elo rungs and no way to play. This is the field that
   * makes that state visible; without it the card claimed such an engine was
   * healthy while the profile editor reported it as not installed.
   */
  profiles_ready: boolean;
  /**
   * The last install or initialization failure recorded for this engine, or null.
   * Rendered as a dismissible notice on the card; every occurrence is also in
   * the system event log.
   */
  last_failure: EngineFailure | null;
  /**
   * Whether this engine is installed but missing required companion files (a
   * net-backed engine like Maia whose weight download failed): the binary is
   * present yet it cannot play. Drives a "Needs repair" badge and the Repair
   * action in place of the normal profile editor. False for ordinary engines.
   */
  needs_repair: boolean;
  /**
   * Whether a net fetch (repair OR top-up) is possible: the engine is installed,
   * has a repair procedure, and has something to fetch -- either it
   * `needs_repair` (no usable net) or it is usable but still missing some
   * expected nets. Gates the repair/top-up button; the label depends on
   * `needs_repair` + `missing_net_count`.
   */
  can_repair: boolean;
  /**
   * How many expected companion nets are still missing. 0 for a complete or
   * non-net engine. When the engine is usable (not `needs_repair`) but this is
   * > 0, the UI offers a quiet "download N missing weights" top-up rather than
   * an alarming Repair.
   */
  missing_net_count: number;
  /**
   * Whether this engine can be installed on the current device's CPU
   * architecture. False for engines that cannot build/run here (e.g. Berserk on
   * 32-bit ARM). When false the install button is disabled.
   */
  supported: boolean;
  /**
   * Human-readable explanation shown when `supported` is false (e.g. "Berserk is
   * not supported on this device's architecture (armhf). Supported: arm64.").
   * Null when the engine is supported.
   */
  unsupported_reason: string | null;
  /**
   * Whether this engine is built from source and therefore supports the release
   * (git ref) picker. False for the Stockfish system package and any bundled
   * engine without a repository. When false the UI omits the picker.
   */
  source_installable: boolean;
  /**
   * The canonical ref an unspecified install builds: the catalog pin for pinned
   * engines, or the default-branch sentinel ("default") for unpinned engines.
   * Null for non-source engines. This is the picker's default selection.
   */
  recommended_ref: string | null;
  /**
   * The git ref the engine is currently installed from (a tag, a branch, or the
   * "default" sentinel), or null when not installed / not recorded.
   */
  installed_ref: string | null;
  /**
   * Whether this is an operator-added (custom) engine -- one uploaded as a
   * binary or installed from a URL rather than shipped in the catalog. Custom
   * engines are rendered in their own section and have no tier/refs/profiles.
   */
  is_custom?: boolean;
}

/**
 * One selectable git ref for a source-built engine, from
 * GET /api/engines/{name}/refs.
 */
export interface EngineRef {
  /** Value sent back to the install endpoint ("default" for the default branch). */
  ref: string;
  /** Human-readable label (the branch name for the default entry, else the ref). */
  label: string;
  /** "branch" for the default-branch entry, otherwise "tag". */
  kind: 'branch' | 'tag';
  /** The catalog pin or a ref that has ever built successfully on this device. */
  known_working: boolean;
  /** This is the catalog's verified pin. */
  is_pin: boolean;
  /** This is the ref currently installed. */
  installed: boolean;
}

/**
 * Payload from GET /api/engines/{name}/refs driving the release picker.
 */
export interface EngineRefsResponse {
  engine: string;
  source_installable: boolean;
  installed_ref: string | null;
  recommended_ref: string | null;
  default_branch: string | null;
  refs: EngineRef[];
}

/**
 * Connection status for SSE.
 */
export type ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected';

/**
 * Board battery status. Mirrors GET /api/system/battery and the board's
 * `battery_status` SSE event. Battery is read from the board controller in the
 * main process, so values are null until the board has reported a reading.
 */
export interface BatteryStatus {
  /** Battery level on the board's 0-20 scale, or null if unknown. */
  battery_level: number | null;
  /** Battery level as a percentage (0-100), or null if unknown. */
  battery_percent: number | null;
  /** Whether the charger is connected. */
  charger_connected: boolean;
}

/**
 * Live chess clock snapshot. Mirrors GET /api/game/clock and the board's
 * `clock_status` SSE event. The clock counts down in the main process, which
 * broadcasts on every tick and state change; the LiveBoard interpolates the
 * active side locally between events using `synced_at`. Times are null and
 * `timed_mode` is false until the board reports a reading (or for untimed games).
 */
export interface ClockStatus {
  /** White's remaining whole seconds, or null if unknown. */
  white_time: number | null;
  /** Black's remaining whole seconds, or null if unknown. */
  black_time: number | null;
  /** Which side's clock is counting, or null if unknown. */
  active_color: 'white' | 'black' | null;
  /** Whether the countdown is actively running (not paused). */
  is_running: boolean;
  /** Whether the clock is paused. */
  is_paused: boolean;
  /** Whether the game has a running clock (false = untimed). */
  timed_mode: boolean;
  /**
   * Wall-clock epoch seconds when the snapshot was produced on the board, used
   * to age the active side locally. Null when no snapshot has been received.
   */
  synced_at: number | null;
}

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
  created_at: string;
  source: string | null;
}

/**
 * Engine definition from backend.
 */
export interface EngineDefinition {
  name: string;
  display_name: string;
  description: string;
  summary: string;
  installed: boolean;
  has_prebuilt: boolean;
  install_time: string | null;
  /**
   * Whether this engine exposes user-editable personality profiles (a parameter
   * schema served by GET /api/engines/{name}/profiles). Drives whether the
   * "Configure profiles" action is shown. Currently true only for Rodent IV.
   */
  has_profiles: boolean;
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

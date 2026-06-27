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

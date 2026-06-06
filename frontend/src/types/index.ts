/**
 * Shared WebSocket message contract for the Blitzy Chess SPA.
 *
 * This module is the TypeScript mirror of the backend dataclasses in
 * `backend/chess_ai/rooms/protocol.py`. Both sides describe the SAME JSON
 * messages exchanged over `/ws/game` and `/ws/multiplayer`, with `snake_case`
 * keys and a `type` discriminator on every message. The contract is
 * hand-maintained on both sides (no code generation); keep this file and
 * `protocol.py` in step whenever either changes.
 *
 * Conventions mirrored from the backend:
 *   - Keys are `snake_case` exactly as they appear on the wire.
 *   - Every message carries a literal `type` discriminant, so the
 *     {@link ClientMessage} and {@link ServerMessage} unions are discriminated
 *     unions that narrow on `type`.
 *   - Fields the backend declares as `T | None = None` are modeled as
 *     `field?: T | null`: the JSON includes them as `null` on the wire, and
 *     clients constructing inbound messages may omit them.
 *
 * @module types
 */

/**
 * A player/side color. Mirrors the backend's `"white"` / `"black"` strings.
 */
export type Color = 'white' | 'black';

/**
 * Whose move it is. Identical to {@link Color}; aliased for readable field types.
 */
export type Turn = Color;

/**
 * Room/game lifecycle status. Mirrors `StateMessage.status`.
 */
export type GameStatus = 'waiting' | 'active' | 'finished';

/**
 * Terminal result kind. Mirrors `GameOverMessage.result`.
 */
export type GameResult = 'checkmate' | 'stalemate' | 'draw' | 'resignation' | 'timeout';

/**
 * Closed set of {@link ErrorMessage.code} values. Mirrors the backend
 * `ErrorCode` `StrEnum` member values one-for-one.
 */
export type ErrorCode =
  | 'illegal_move'
  | 'not_your_turn'
  | 'room_not_found'
  | 'room_full'
  | 'invalid_message'
  | 'game_not_active'
  | 'reconnect_failed';

/**
 * Runtime-accessible map of the canonical error codes, mirroring the backend
 * `ErrorCode` enum so callers can reference codes by name (e.g.
 * `ERROR_CODES.ILLEGAL_MOVE`) instead of repeating string literals.
 */
export const ERROR_CODES = {
  ILLEGAL_MOVE: 'illegal_move',
  NOT_YOUR_TURN: 'not_your_turn',
  ROOM_NOT_FOUND: 'room_not_found',
  ROOM_FULL: 'room_full',
  INVALID_MESSAGE: 'invalid_message',
  GAME_NOT_ACTIVE: 'game_not_active',
  RECONNECT_FAILED: 'reconnect_failed',
} as const satisfies Record<string, ErrorCode>;

/**
 * The squares of the most recent move, mirroring `StateMessage.last_move`
 * (`{"from_square": str, "to_square": str}`).
 */
export interface LastMove {
  from_square: string;
  to_square: string;
}

// ===========================================================================
// Client -> Server (inbound) messages
// ===========================================================================

/** Request to create a new multiplayer room. No payload fields. */
export interface CreateRoomMessage {
  type: 'create_room';
}

/** Request to join an existing room by its 6-character code. */
export interface JoinRoomMessage {
  type: 'join_room';
  code: string;
}

/**
 * A move submitted by a client. The server reconstructs and validates the move
 * from `from_square`, `to_square`, and `promotion`; `san` is carried only for
 * debugging/telemetry and is never trusted by the backend.
 */
export interface MoveMessage {
  type: 'move';
  from_square: string;
  to_square: string;
  promotion?: string | null;
  san?: string | null;
}

/** Request to restore a player into an existing room after a disconnect. */
export interface ReconnectMessage {
  type: 'reconnect';
  code: string;
  player_token: string;
}

/**
 * Request to resign the current game. The connection identifies the player;
 * `player_token` is optional and used only when the transport prefers explicit
 * identification.
 */
export interface ResignMessage {
  type: 'resign';
  player_token?: string | null;
}

// ===========================================================================
// Server -> Client (outbound) messages
// ===========================================================================

/** Response to `create_room`. `color` is the creator's assigned color. */
export interface RoomCreatedMessage {
  type: 'room_created';
  code: string;
  color: Color;
  player_token: string;
}

/** Response to `join_room`. `color` is the joiner's assigned color. */
export interface RoomJoinedMessage {
  type: 'room_joined';
  code: string;
  color: Color;
  player_token: string;
}

/**
 * Authoritative position snapshot. `move_history` is the ordered list of SAN
 * strings (e.g. `["e4", "e5", "Nf3"]`) the frontend pairs into numbered rows.
 */
export interface StateMessage {
  type: 'state';
  fen: string;
  move_history: string[];
  turn: Turn;
  status: GameStatus;
  in_check: boolean;
  last_move?: LastMove | null;
  winner?: Color | null;
  result?: string | null;
}

/**
 * AI search-progress update, shared by `/ws/game` and `/ws/multiplayer`.
 * `evaluation` is in centipawns from White's point of view.
 */
export interface AiThinkingMessage {
  type: 'ai_thinking';
  depth: number;
  evaluation: number;
  pv: string[];
  nodes: number;
  time_s?: number | null;
  nps?: number | null;
  mate_in?: number | null;
  seldepth?: number | null;
}

/** End-of-game notification. `winner` is `null` for draws and stalemate. */
export interface GameOverMessage {
  type: 'game_over';
  result: GameResult;
  winner: Color | null;
  reason: string;
}

/** Error notification. `code` is drawn from {@link ErrorCode}. */
export interface ErrorMessage {
  type: 'error';
  code: ErrorCode;
  message: string;
}

// ===========================================================================
// Discriminated unions
// ===========================================================================

/**
 * Any message a client sends to the server. Narrow on the `type` discriminant.
 */
export type ClientMessage =
  | CreateRoomMessage
  | JoinRoomMessage
  | MoveMessage
  | ReconnectMessage
  | ResignMessage;

/**
 * Any message the server sends to a client. Narrow on the `type` discriminant.
 */
export type ServerMessage =
  | RoomCreatedMessage
  | RoomJoinedMessage
  | StateMessage
  | AiThinkingMessage
  | GameOverMessage
  | ErrorMessage;

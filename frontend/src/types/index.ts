/**
 * Shared TypeScript types for the Blitzy Chess single-page application.
 *
 * This module is the hand-maintained TypeScript mirror of the canonical backend
 * wire contract in `backend/chess_ai/rooms/protocol.py`. Both sides describe the
 * SAME JSON messages exchanged over the two WebSocket endpoints (`/ws/game` and
 * `/ws/multiplayer`). It is imported by `../App.tsx`, the components under
 * `../components/`, the hooks under `../hooks/`, and the suites under `../tests/`.
 *
 * Authority: `protocol.py` is authoritative (AAP §0.4.2 "Message contract
 * parity"). The backend serializes every message with
 * `json.dumps(dataclasses.asdict(msg))`, so each JSON key equals the Python
 * dataclass field name verbatim, in `snake_case`. There is NO camelCase
 * conversion on the wire; the field names below match the backend one for one.
 *
 * Wire conventions mirrored from the backend:
 *   - Keys are `snake_case` exactly as they appear on the wire (e.g.
 *     `from_square`, `to_square`, `player_token`, `move_history`, `in_check`,
 *     `last_move`, `time_s`, `mate_in`). Never `fromSquare`, `playerToken`, etc.
 *   - Every message carries a literal `type` string discriminant, so the
 *     {@link ClientMessage} and {@link ServerMessage} unions are discriminated
 *     unions a consumer can narrow with an exhaustive `switch (msg.type)`.
 *   - Colors, turn, and winner are the full strings `'white'` / `'black'`
 *     (never `'w'` / `'b'`); `winner` may be `null`.
 *   - Squares are lowercase algebraic strings such as `'e2'` or `'g8'`.
 *     Promotion is a lowercase piece letter (`'q' | 'r' | 'b' | 'n'`) or `null`.
 *   - Move squares use `from_square` / `to_square` (`from` is a Python keyword,
 *     so the backend never uses it as a field name; this file mirrors that).
 *
 * Purity: this is a pure type-declaration module. It exports types and
 * interfaces only — no classes, no functions, no runtime values — so it erases
 * to an empty module at build time and contributes no JavaScript to the bundle.
 * It has no imports; the backend references above are cross-language parity
 * pointers, not code dependencies.
 *
 * @module types
 */

// ===========================================================================
// Helper / shared scalar types
// ===========================================================================

/** A player/side color. Mirrors the backend's `"white"` / `"black"` strings. */
export type Color = 'white' | 'black';

/**
 * AI difficulty tier selected on the mode screen and passed to `/ws/game`.
 * Each tier maps (on the backend) to a search depth and per-move time budget:
 * Easy (4 / 3s), Medium (6 / 8s), Hard (8 / 15s).
 */
export type Difficulty = 'easy' | 'medium' | 'hard';

/**
 * Promotion piece letter. Lowercase to match python-chess / the wire contract:
 * queen, rook, bishop, knight.
 */
export type PromotionPiece = 'q' | 'r' | 'b' | 'n';

/** Room/game lifecycle status. Mirrors `StateMessage.status`. */
export type GameStatus = 'waiting' | 'active' | 'finished';

/** Terminal result kind. Mirrors `GameOverMessage.result`. */
export type GameResult = 'checkmate' | 'stalemate' | 'draw' | 'resignation' | 'timeout';

/**
 * `ErrorMessage.code` values. The first five are the canonical, commonly
 * handled codes; the trailing `(string & {})` keeps editor autocomplete for
 * those members while still accepting any other server-sent string, so a future
 * or less-common backend code never breaks compilation. The backend
 * `ErrorCode` `StrEnum` additionally defines `'game_not_active'` and
 * `'reconnect_failed'`, both of which type-check via the open fallback.
 */
export type ErrorCode =
  | 'illegal_move'
  | 'not_your_turn'
  | 'room_not_found'
  | 'room_full'
  | 'invalid_message'
  | (string & {});

/**
 * The squares of the most recent move, mirroring `StateMessage.last_move`
 * (`{"from_square": str, "to_square": str}`). The backend `manager.build_state`
 * emits exactly these two keys via `chess.square_name(...)`.
 */
export interface SquareMove {
  from_square: string;
  to_square: string;
}

// ===========================================================================
// Client -> Server (outbound) messages
// ===========================================================================

/**
 * A move submitted by a client. The server reconstructs and validates the move
 * from `from_square`, `to_square`, and `promotion` (via `board.is_legal`); it is
 * the sole authority on legality. `promotion` is `null`/omitted for non-promoting
 * moves. The backend tolerates an optional `san` telemetry field, but the
 * frontend never sends it, so it is intentionally not modeled here.
 */
export interface MoveMessage {
  type: 'move';
  from_square: string;
  to_square: string;
  promotion?: PromotionPiece | null;
}

/** Request to create a new multiplayer room. No payload beyond the discriminant. */
export interface CreateRoomMessage {
  type: 'create_room';
}

/** Request to join an existing room by its 6-character code. */
export interface JoinRoomMessage {
  type: 'join_room';
  code: string;
}

/**
 * Request to restore a player into an existing room after a disconnect.
 * `player_token` is the token the client received from `room_created` or
 * `room_joined`.
 */
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
  player_token?: string;
}

// ===========================================================================
// Server -> Client (inbound) messages
// ===========================================================================

/**
 * Authoritative position snapshot. `move_history` is the ordered list of SAN
 * strings (e.g. `['e4', 'e5', 'Nf3']`) that `MoveHistory.tsx` pairs into
 * numbered rows. `last_move` is `null` before any move is made. Terminal info is
 * carried primarily by {@link GameOverMessage}, so `winner` / `result` are
 * optional here and present only on snapshots that also report a result.
 */
export interface StateMessage {
  type: 'state';
  fen: string;
  move_history: string[];
  turn: Color;
  status: GameStatus;
  in_check: boolean;
  last_move: SquareMove | null;
  winner?: Color | null;
  result?: string;
}

/**
 * AI search-progress update, emitted by `/ws/game` as the engine thinks.
 * `evaluation` is in centipawns from White's point of view (positive means
 * White is better). `pv` is the principal variation as SAN strings. `mate_in`
 * is `null` when there is no forced mate.
 */
export interface AiThinkingMessage {
  type: 'ai_thinking';
  depth: number;
  evaluation: number;
  pv: string[];
  nodes: number;
  time_s?: number;
  nps?: number;
  mate_in?: number | null;
  seldepth?: number;
}

/**
 * End-of-game notification. `winner` is `null` for draws and stalemate.
 * `reason` is a short human-readable string such as "Black wins by checkmate",
 * rendered by `GameOverOverlay.tsx`.
 */
export interface GameOverMessage {
  type: 'game_over';
  result: GameResult;
  winner: Color | null;
  reason: string;
}

/**
 * Response to `create_room`. The creator is always seated as White, so `color`
 * is the literal `'white'`. `player_token` identifies the creator on reconnect.
 */
export interface RoomCreatedMessage {
  type: 'room_created';
  code: string;
  color: 'white';
  player_token: string;
}

/**
 * Response to `join_room`. The joiner is always seated as Black, so `color` is
 * the literal `'black'`. `player_token` identifies the joiner on reconnect.
 */
export interface RoomJoinedMessage {
  type: 'room_joined';
  code: string;
  color: 'black';
  player_token: string;
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
 * Any message the server sends to a client. Hooks narrow on the `type`
 * discriminant; an exhaustive `switch (msg.type)` covers every member.
 */
export type ServerMessage =
  | StateMessage
  | AiThinkingMessage
  | GameOverMessage
  | RoomCreatedMessage
  | RoomJoinedMessage
  | ErrorMessage;

/**
 * Any message a client sends to the server. Hooks narrow on the `type`
 * discriminant before serializing the payload onto the socket.
 */
export type ClientMessage =
  | MoveMessage
  | CreateRoomMessage
  | JoinRoomMessage
  | ReconnectMessage
  | ResignMessage;

/** Combined union of every message in either direction. */
export type WebSocketMessage = ServerMessage | ClientMessage;

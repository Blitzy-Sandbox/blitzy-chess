/**
 * useMultiplayerWebSocket — React transport hook for the two-human multiplayer
 * channel (`/ws/multiplayer`).
 *
 * This hook owns the entire client side of the multiplayer protocol: it opens a
 * single WebSocket to the backend, drives the room lifecycle (create / join /
 * reconnect / resign), submits moves, and surfaces the authoritative server
 * state to `../App.tsx`. It is the ONLY transport path for multiplayer room
 * actions and moves — there is no HTTP/REST fallback (canonical constraint C16);
 * the backend python-chess board is authoritative and validates every move with
 * `board.is_legal()` before relaying it.
 *
 * Transport invariants:
 *   - The WebSocket URL is RELATIVE and resolved at call-time from
 *     `window.location` (constraint C17), so the same code reaches the backend
 *     through the Vite dev-server proxy in development and the co-served origin
 *     in production. The host is never hard-coded.
 *   - The `WebSocket` constructor is resolved at call-time (never captured at
 *     module scope), so a test double installed via `vi.stubGlobal` is honored.
 *   - The wire contract is `snake_case` with a `type` discriminant, mirroring
 *     `backend/chess_ai/rooms/protocol.py`. The single camelCase concession is
 *     the returned `room.playerToken`, a hook-return convenience consumed by
 *     `App.tsx`; it is mapped from the wire's `player_token` at the boundary.
 *   - Cleanup detaches every socket handler before closing, so React 18
 *     StrictMode's mount/unmount/remount never leaks a socket or triggers a
 *     stray reconnect.
 *
 * @module hooks/useMultiplayerWebSocket
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  Color,
  CreateRoomMessage,
  ErrorMessage,
  GameOverMessage,
  JoinRoomMessage,
  MoveMessage,
  ReconnectMessage,
  ResignMessage,
  ServerMessage,
  StateMessage,
} from '../types';

/** Initial reconnect backoff, in milliseconds. */
const BASE_RECONNECT_MS = 500;
/** Maximum reconnect backoff, in milliseconds (the exponential cap). */
const MAX_RECONNECT_MS = 10_000;
/**
 * `localStorage` key prefix for the per-room player token. The full key is
 * `${TOKEN_KEY_PREFIX}${code}`, so each room code persists its own token and a
 * future reconnect can restore the player into the correct seat.
 */
const TOKEN_KEY_PREFIX = 'blitzy-chess:mp-token:';

/**
 * The outbound (client -> server) messages this hook serializes through the
 * shared {@link send} guard. `ReconnectMessage` is intentionally excluded: it is
 * sent directly from inside `onopen` against the just-opened socket, never via
 * the public callbacks.
 */
type OutboundMessage = CreateRoomMessage | JoinRoomMessage | MoveMessage | ResignMessage;

/**
 * The local, hook-facing view of the room a player currently occupies. Built
 * from a `room_created` / `room_joined` server message. `playerToken` is the
 * camelCase mapping of the wire's `player_token`.
 */
export interface RoomInfo {
  code: string;
  color: Color;
  playerToken: string;
}

/**
 * The stable shape returned by {@link useMultiplayerWebSocket}. Shared verbatim
 * with `../App.tsx`; the field names and callback signatures must not drift.
 */
export interface UseMultiplayerWebSocketResult {
  /** Latest authoritative position snapshot, or `null` before the first state. */
  state: StateMessage | null;
  /** Terminal result, or `null` while the game is still in progress. */
  gameOver: GameOverMessage | null;
  /** Most recent server error (e.g. `room_not_found`), or `null`. */
  error: ErrorMessage | null;
  /** Whether the socket is currently open. */
  connected: boolean;
  /** The room this client occupies, or `null` before create/join succeeds. */
  room: RoomInfo | null;
  /** Ask the server to create a new room; the creator is seated as White. */
  createRoom(): void;
  /** Ask the server to join an existing room by its 6-character code. */
  joinRoom(code: string): void;
  /** Submit a move; `promotion` is a lowercase piece letter when promoting. */
  sendMove(from: string, to: string, promotion?: string): void;
  /** Resign the current game, identifying this client by its room token. */
  resign(): void;
}

/**
 * Persist a room's player token to `localStorage`, keyed by room code. Best
 * effort: storage may be unavailable (private mode, disabled cookies, quota),
 * so any failure is swallowed — the token also lives in `roomRef` for the life
 * of the session, which is what reconnect actually reads.
 */
function persistToken(code: string, token: string): void {
  try {
    localStorage.setItem(`${TOKEN_KEY_PREFIX}${code}`, token);
  } catch {
    // localStorage may be unavailable; persistence is best-effort.
  }
}

/**
 * Owns the multiplayer WebSocket connection and the room/game state derived from
 * it. Connects on mount, waits for an explicit {@link UseMultiplayerWebSocketResult.createRoom}
 * or {@link UseMultiplayerWebSocketResult.joinRoom} (it never auto-joins), and
 * transparently reconnects with capped exponential backoff on unexpected drops.
 */
export function useMultiplayerWebSocket(): UseMultiplayerWebSocketResult {
  const [state, setState] = useState<StateMessage | null>(null);
  const [gameOver, setGameOver] = useState<GameOverMessage | null>(null);
  const [error, setError] = useState<ErrorMessage | null>(null);
  const [connected, setConnected] = useState<boolean>(false);
  const [room, setRoom] = useState<RoomInfo | null>(null);

  // Non-reactive references that must survive re-renders without retriggering
  // the connection effect: the live socket, the pending reconnect timer, the
  // current backoff attempt count, and the room we belong to (read on reopen).
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptsRef = useRef<number>(0);
  const roomRef = useRef<RoomInfo | null>(null);

  /**
   * Serialize and send an outbound message, but only when the socket is open.
   * Reading `socketRef.current` (rather than closing over a socket) keeps every
   * caller bound to the live connection after a reconnect swaps the instance.
   */
  const send = useCallback((message: OutboundMessage): void => {
    const ws = socketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return;
    }
    try {
      ws.send(JSON.stringify(message));
    } catch {
      // Socket closed between the guard and the send; drop the message.
    }
  }, []);

  const createRoom = useCallback((): void => {
    send({ type: 'create_room' });
  }, [send]);

  const joinRoom = useCallback(
    (code: string): void => {
      send({ type: 'join_room', code });
    },
    [send],
  );

  const sendMove = useCallback(
    (from: string, to: string, promotion?: string): void => {
      send({
        type: 'move',
        from_square: from,
        to_square: to,
        promotion: (promotion ?? null) as MoveMessage['promotion'],
      });
    },
    [send],
  );

  const resign = useCallback((): void => {
    send({ type: 'resign', player_token: roomRef.current?.playerToken });
  }, [send]);

  useEffect(() => {
    // `cancelled` is the per-effect-run guard: once cleanup sets it, the stale
    // closure's socket handlers become no-ops, so StrictMode's first (discarded)
    // mount can never schedule a reconnect or mutate state after teardown.
    let cancelled = false;

    const clearReconnectTimer = (): void => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const handleMessage = (event: MessageEvent): void => {
      let message: ServerMessage;
      try {
        message = JSON.parse(event.data) as ServerMessage;
      } catch {
        // Ignore frames that are not valid JSON; the server only sends JSON.
        return;
      }
      switch (message.type) {
        case 'room_created':
        case 'room_joined': {
          // The wire `player_token` (snake_case) becomes the hook-facing
          // `playerToken` (camelCase) here, at the single mapping boundary.
          const info: RoomInfo = {
            code: message.code,
            color: message.color,
            playerToken: message.player_token,
          };
          roomRef.current = info;
          setRoom(info);
          persistToken(info.code, info.playerToken);
          break;
        }
        case 'state':
          setState(message);
          break;
        case 'game_over':
          setGameOver(message);
          break;
        case 'error':
          setError(message);
          break;
        default:
          // Other server messages (e.g. ai_thinking) are not consumed here.
          break;
      }
    };

    const connect = (): void => {
      if (cancelled) {
        return;
      }
      // RELATIVE URL resolved at call-time (C17): never hard-code the host.
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const url = `${protocol}://${window.location.host}/ws/multiplayer`;
      // `WebSocket` is referenced at call-time so a stubbed global is honored.
      const ws = new WebSocket(url);
      socketRef.current = ws;

      ws.onopen = (): void => {
        if (cancelled) {
          return;
        }
        attemptsRef.current = 0;
        setConnected(true);
        // If we already held a room and the socket dropped, ask the server to
        // restore us (it replays the current state: FEN + move history). On the
        // very first connect `roomRef.current` is null, so nothing is sent.
        const current = roomRef.current;
        if (current) {
          const message: ReconnectMessage = {
            type: 'reconnect',
            code: current.code,
            player_token: current.playerToken,
          };
          try {
            ws.send(JSON.stringify(message));
          } catch {
            // Resume request failed; the close handler will retry.
          }
        }
      };

      ws.onmessage = (event: MessageEvent): void => {
        if (cancelled) {
          return;
        }
        handleMessage(event);
      };

      ws.onerror = (): void => {
        // Errors surface as a subsequent close; the close handler reconnects.
      };

      ws.onclose = (): void => {
        if (cancelled) {
          return;
        }
        setConnected(false);
        // Capped exponential backoff: delay = min(cap, BASE * 2 ** attempts).
        const delay = Math.min(MAX_RECONNECT_MS, BASE_RECONNECT_MS * 2 ** attemptsRef.current);
        attemptsRef.current += 1;
        reconnectTimerRef.current = setTimeout(connect, delay);
      };
    };

    connect();

    return (): void => {
      // Detach handlers BEFORE closing so the close never schedules a reconnect,
      // then tear down the timer and socket and reset the connected flag.
      cancelled = true;
      clearReconnectTimer();
      const ws = socketRef.current;
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
        socketRef.current = null;
      }
      setConnected(false);
    };
  }, []);

  return { state, gameOver, error, connected, room, createRoom, joinRoom, sendMove, resign };
}

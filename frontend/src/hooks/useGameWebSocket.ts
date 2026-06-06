/**
 * useGameWebSocket — React transport hook for the single-player-vs-AI channel
 * (`/ws/game`).
 *
 * This hook owns the entire client side of the human-vs-AI protocol: it opens a
 * single WebSocket to the backend for the chosen difficulty tier (and optional
 * human color), submits the human's moves, streams the engine's search-progress
 * updates, and surfaces the authoritative server state to `../App.tsx`. It is
 * the ONLY transport path for game moves — there is no HTTP/REST fallback
 * (canonical constraint C16); the backend python-chess board is authoritative
 * and validates every move with `board.is_legal()` before applying it, so the
 * client never decides legality.
 *
 * Transport invariants:
 *   - The WebSocket URL is RELATIVE and resolved at call-time from
 *     `window.location` (constraint C17), so the same code reaches the backend
 *     through the Vite dev-server proxy in development (`/ws` is proxied to the
 *     backend with `ws: true`) and the co-served origin in production. The host
 *     is never hard-coded.
 *   - The `WebSocket` constructor is resolved at call-time (never captured at
 *     module scope), so a test double installed via `vi.stubGlobal('WebSocket',
 *     ...)` is honored by the suite under `../tests/`.
 *   - The wire contract is `snake_case` with a `type` discriminant, mirroring
 *     `backend/chess_ai/rooms/protocol.py`. Move payloads use `from_square` /
 *     `to_square` (never `from` / `to`), and `promotion` defaults to `null`.
 *   - Cleanup detaches every socket handler before closing, so React 18
 *     StrictMode's mount/unmount/remount never leaks a socket or triggers a
 *     stray reconnect.
 *
 * Protocol flow (server side: `api/game_ws.py`):
 *   - On connect the server sends the initial `state`. After the human's move
 *     the server streams that applied `state`, then zero or more `ai_thinking`
 *     updates while the engine searches, then the AI's resulting `state`.
 *   - `game_over` ends the game; `error` (e.g. `illegal_move`) reports a
 *     rejected move without mutating the position.
 *   - There is no server-side resume for AI games: a reconnect simply
 *     re-establishes the socket and the server replies with a fresh initial
 *     `state`. `newGame()` exploits this by closing the socket and opening a
 *     new one — the backend treats a new connection as a new game.
 *
 * The rationale for the design choices here (relative call-time URL, the
 * `generation`-driven new-game reconnect, the detach-before-close teardown) is
 * recorded in docs/decision-log.md, not in these comments, per the
 * Explainability rule.
 *
 * @module hooks/useGameWebSocket
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  AiThinkingMessage,
  Color,
  Difficulty,
  ErrorMessage,
  GameOverMessage,
  MoveMessage,
  ResignMessage,
  ServerMessage,
  StateMessage,
} from '../types';

/** Initial reconnect backoff, in milliseconds. */
const BASE_RECONNECT_MS = 500;
/** Maximum reconnect backoff, in milliseconds (the exponential cap). */
const MAX_RECONNECT_MS = 10_000;

/**
 * Inputs to {@link useGameWebSocket}. Shared verbatim with `../App.tsx`; the
 * field names must not drift. Changing either field reconnects the hook to a
 * fresh `/ws/game` session (the values are part of the connection effect's
 * dependency list and are encoded into the query string).
 */
export interface UseGameWebSocketParams {
  /** AI difficulty tier; maps (backend-side) to a search depth and time budget. */
  difficulty: Difficulty;
  /**
   * The color the human plays. Optional: when omitted the backend assigns a
   * default (the human plays White), so the query string drops the `color`
   * parameter entirely rather than sending an empty value.
   */
  humanColor?: Color;
}

/**
 * The stable shape returned by {@link useGameWebSocket}. Shared verbatim with
 * `../App.tsx`; the field names and callback signatures must not drift.
 */
export interface UseGameWebSocketResult {
  /** Latest authoritative position snapshot, or `null` before the first state. */
  state: StateMessage | null;
  /** Most recent AI search-progress update, or `null` when the engine is idle. */
  aiThinking: AiThinkingMessage | null;
  /** Terminal result, or `null` while the game is still in progress. */
  gameOver: GameOverMessage | null;
  /** Most recent server error (e.g. `illegal_move`), or `null`. */
  error: ErrorMessage | null;
  /** Whether the socket is currently open. */
  connected: boolean;
  /** Submit a move; `promotion` is a lowercase piece letter when promoting. */
  sendMove(from: string, to: string, promotion?: string): void;
  /** Resign the current game; the connection identifies the player. */
  resign(): void;
  /** Abandon the current game and start a fresh one on a brand-new socket. */
  newGame(): void;
}

/**
 * Owns the AI-game WebSocket connection and the game state derived from it.
 * Connects on mount, reconnects with capped exponential backoff on unexpected
 * drops, and reconnects deliberately (as a fresh game) when {@link
 * UseGameWebSocketResult.newGame} is called or when `difficulty` / `humanColor`
 * change.
 *
 * @param params - The difficulty tier and optional human color for this game.
 * @returns The reactive game state plus the move / resign / new-game callbacks.
 */
export function useGameWebSocket(params: UseGameWebSocketParams): UseGameWebSocketResult {
  const { difficulty, humanColor } = params;

  // Reactive state surfaced to consumers. Each inbound server message type maps
  // to exactly one of these setters (see `handleMessage` below).
  const [state, setState] = useState<StateMessage | null>(null);
  const [aiThinking, setAiThinking] = useState<AiThinkingMessage | null>(null);
  const [gameOver, setGameOver] = useState<GameOverMessage | null>(null);
  const [error, setError] = useState<ErrorMessage | null>(null);
  const [connected, setConnected] = useState<boolean>(false);
  // A monotonically increasing counter that, when bumped, re-runs the
  // connection effect to open a fresh socket. This is how `newGame()` starts a
  // new game without any custom wire message.
  const [generation, setGeneration] = useState<number>(0);

  // Non-reactive references that must survive re-renders without retriggering
  // the connection effect: the live socket, the pending reconnect timer, and
  // the current backoff attempt count.
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptsRef = useRef<number>(0);

  /**
   * Serialize and send an outbound message, but only when the socket is open.
   * Reading `socketRef.current` (rather than closing over a socket instance)
   * keeps every caller bound to the live connection after a reconnect swaps the
   * instance. `WebSocket.OPEN` is read at call-time so a stubbed global is
   * honored in tests.
   */
  const send = useCallback((message: MoveMessage | ResignMessage): void => {
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

  const sendMove = useCallback(
    (from: string, to: string, promotion?: string): void => {
      // The public signature accepts a plain `string`; narrow to the wire
      // contract's promotion union at this single boundary. No move ever sends
      // an undefined promotion — it is `null` when not promoting.
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
    send({ type: 'resign' });
  }, [send]);

  const newGame = useCallback((): void => {
    // Clear the previous game's state immediately so the UI does not flash the
    // old position, reset the backoff counter, then bump `generation` to force
    // the connection effect to tear down the old socket and open a fresh one.
    setState(null);
    setAiThinking(null);
    setGameOver(null);
    setError(null);
    attemptsRef.current = 0;
    setGeneration((value) => value + 1);
  }, []);

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
      // Narrow the discriminated union on `type` and route each inbound message
      // to its setter. `room_*` messages never arrive on `/ws/game`, so they
      // fall through to the default and are ignored.
      switch (message.type) {
        case 'state':
          setState(message);
          // The authoritative position has arrived. In the AI flow the engine's
          // `ai_thinking` updates stream only BETWEEN the human-applied `state`
          // and the AI-applied `state`, so clearing here returns `aiThinking` to
          // `null` once the search that produced this position is complete. The
          // SidePanel "AI thinking…" block is gated on a truthy `aiThinking`, so
          // this guarantees it shows only while a search is actually streaming
          // and never lingers after the AI move lands.
          setAiThinking(null);
          break;
        case 'ai_thinking':
          setAiThinking(message);
          break;
        case 'game_over':
          setGameOver(message);
          // The game has ended and the engine is idle; clear any lingering
          // search progress so the thinking indicator never persists past the
          // result.
          setAiThinking(null);
          break;
        case 'error':
          setError(message);
          break;
        default:
          break;
      }
    };

    const connect = (): void => {
      if (cancelled) {
        return;
      }
      // RELATIVE URL resolved at call-time (C17): never hard-code the host. The
      // optional `color` parameter is omitted entirely when no human color is
      // set, so the backend applies its default.
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const query = `difficulty=${encodeURIComponent(difficulty)}${
        humanColor ? `&color=${encodeURIComponent(humanColor)}` : ''
      }`;
      const url = `${protocol}://${window.location.host}/ws/game?${query}`;
      // `WebSocket` is referenced at call-time so a stubbed global is honored.
      const ws = new WebSocket(url);
      socketRef.current = ws;

      ws.onopen = (): void => {
        if (cancelled) {
          return;
        }
        // A successful open resets the backoff so the next unexpected drop
        // starts from BASE again.
        attemptsRef.current = 0;
        setConnected(true);
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
        // This branch runs only on an UNEXPECTED close — intentional closes
        // (newGame / unmount) detach this handler first, so they never schedule
        // a reconnect.
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
  }, [difficulty, humanColor, generation]);

  return { state, aiThinking, gameOver, error, connected, sendMove, resign, newGame };
}

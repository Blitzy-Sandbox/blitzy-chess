/**
 * App — top-level screen router and game-screen composition root for the Blitzy
 * Chess single-page application.
 *
 * This is the React root mounted by `main.tsx`. It is a small, state-driven
 * router over the application's five screens (AAP §0.5.3) — with NO router
 * library — implemented as a `useState` discriminated-union state machine:
 *
 *   1. Mode select   → choose an AI difficulty tier, play online, or watch self-play
 *   2. AI game       → board + side panel, driven by `/ws/game`
 *   3. Online lobby   ┐ one screen ({@link OnlineScreen}) so the multiplayer
 *   4. Multiplayer   ┘ socket survives the lobby → game transition
 *   5. Self-play     → the AI-vs-AI demonstration ({@link SelfPlayView})
 *
 * Composition root for the games. There are no dedicated "AI game" or
 * "multiplayer game" screen components, so App composes them here from
 * {@link GameBoard} + {@link SidePanel} + the relevant WebSocket hook +
 * {@link useGameState} (the display mirror) + {@link PromotionDialog} +
 * {@link GameOverOverlay}. {@link ModeSelect}, {@link OnlineLobby}, and
 * {@link SelfPlayView} are standalone full screens.
 *
 * Rules of Hooks. Each game screen owns its own WebSocket and mirror hooks, so
 * those hooks are placed in dedicated child components ({@link AiGameScreen},
 * {@link OnlineScreen}) that are mounted only when active — App never calls a
 * game hook behind a conditional. These children stay in THIS file (the AAP
 * fixes the file list); only {@link App} is exported, which also keeps the file
 * fast-refresh friendly (`react-refresh/only-export-components`).
 *
 * Load-bearing constraints upheld by the screens composed here:
 *   - C1 (server authoritative): every rendered position is the server's
 *     {@link StateMessage}.fen. The {@link useGameState} chess.js mirror is
 *     display/SAN/highlight only and is synced one-way FROM that FEN — it never
 *     decides legality.
 *   - C15 (board only via react-chessboard): the board is drawn exclusively by
 *     {@link GameBoard}; App renders no board of its own.
 *   - C16 (WebSocket only for moves): user moves flow solely through the hooks'
 *     `sendMove`; App issues no HTTP/REST for game actions.
 *
 * The project uses the JSX automatic runtime, so React is not imported as a
 * namespace; only the named hooks are. The rationale for design choices lives in
 * docs/decision-log.md, per the Explainability rule, not in these comments.
 *
 * @module App
 */
import { useCallback, useEffect, useState } from 'react';

import { GameBoard } from './components/GameBoard';
import { GameOverOverlay } from './components/GameOverOverlay';
import { ModeSelect } from './components/ModeSelect';
import { OnlineLobby } from './components/OnlineLobby';
import { PromotionDialog } from './components/PromotionDialog';
import { SelfPlayView } from './components/SelfPlayView';
import { SidePanel } from './components/SidePanel';
import { useGameState } from './hooks/useGameState';
import { useGameWebSocket } from './hooks/useGameWebSocket';
import { useMultiplayerWebSocket } from './hooks/useMultiplayerWebSocket';
import type {
  AiThinkingMessage,
  Color,
  Difficulty,
  ErrorMessage,
  GameOverMessage,
  StateMessage,
} from './types';

/** Design maximum board width in pixels (AAP §0.5.3). GameBoard also caps to this. */
const MAX_BOARD_WIDTH = 640;

/** Horizontal breathing room subtracted from the viewport when sizing the board. */
const BOARD_VIEWPORT_MARGIN = 32;

/** Minimum board width so the board never collapses on very narrow viewports. */
const MIN_BOARD_WIDTH = 240;

/** How long a transient server error stays visible in the toast, in milliseconds. */
const ERROR_TOAST_MS = 4000;

/**
 * Map the server's snake_case `last_move` (`{ from_square, to_square }`) to the
 * camelCase `{ from, to }` shape {@link GameBoard} expects. Returns `null` before
 * the first move of the game.
 *
 * @param state - The latest authoritative state, or `null`.
 * @returns The last move in `{ from, to }` form, or `null`.
 */
function toBoardLastMove(state: StateMessage | null): { from: string; to: string } | null {
  if (!state || !state.last_move) {
    return null;
  }
  return { from: state.last_move.from_square, to: state.last_move.to_square };
}

/**
 * Build the human-readable status line shown in the {@link SidePanel} from the
 * authoritative server state.
 *
 * @param state - The latest authoritative state, or `null` before it arrives.
 * @param connected - Whether the WebSocket is currently open.
 * @param myColor - The local player's side.
 * @param opponentLabel - What to call the other side ("AI" or "Opponent").
 * @returns The status string.
 */
function deriveStatus(
  state: StateMessage | null,
  connected: boolean,
  myColor: Color,
  opponentLabel: string,
): string {
  if (!state) {
    return connected ? 'Waiting for game…' : 'Connecting…';
  }
  if (state.status === 'waiting') {
    return 'Waiting for opponent…';
  }
  if (state.status === 'finished') {
    return 'Game over';
  }
  const myTurn = state.turn === myColor;
  if (state.in_check) {
    return myTurn ? 'Your move — you are in check' : `${opponentLabel} to move — check`;
  }
  return myTurn ? 'Your move' : `${opponentLabel} to move`;
}

/**
 * Measure a board width from the viewport, clamped to the design bounds.
 * react-chessboard renders at a fixed pixel size, so the board is sized from the
 * viewport (less a small margin) rather than by CSS alone.
 *
 * @returns A board width in pixels within `[MIN_BOARD_WIDTH, MAX_BOARD_WIDTH]`.
 */
function measureBoardWidth(): number {
  const available = window.innerWidth - BOARD_VIEWPORT_MARGIN;
  return Math.max(MIN_BOARD_WIDTH, Math.min(available, MAX_BOARD_WIDTH));
}

/**
 * Reactive, viewport-derived board width capped at the design maximum. Re-measures
 * on window resize so the board stays within small screens and never overflows.
 *
 * @returns The current board width in pixels.
 */
function useBoardWidth(): number {
  const [width, setWidth] = useState<number>(measureBoardWidth);

  useEffect(() => {
    const onResize = (): void => setWidth(measureBoardWidth());
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  return width;
}

/**
 * A fixed, top-left "back to menu" control overlaid on the game and lobby
 * screens, whose full-screen children render no back affordance of their own.
 * `z-20` keeps it beneath the `z-50` modal overlays (promotion / game-over).
 *
 * @param props.onClick - Invoked to leave the current screen.
 * @param props.label - The destination label; defaults to "Menu".
 */
function BackButton({ onClick, label = 'Menu' }: { onClick: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Back to ${label.toLowerCase()}`}
      className="fixed left-4 top-4 z-20 rounded-md bg-gray-700 px-3 py-2 text-sm font-medium text-gray-100 shadow-lg hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 motion-safe:transition-colors"
    >
      ← {label}
    </button>
  );
}

/**
 * A transient, auto-dismissing toast surfacing a non-fatal server error (e.g. an
 * illegal move or a room error). The authoritative board state is unaffected
 * (C1); this only informs the user. It re-shows whenever a new error object
 * arrives and hides itself after {@link ERROR_TOAST_MS}.
 *
 * @param props.error - The latest server error, or `null`.
 */
function ErrorToast({ error }: { error: ErrorMessage | null }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!error) {
      setVisible(false);
      return;
    }
    setVisible(true);
    const timer = setTimeout(() => setVisible(false), ERROR_TOAST_MS);
    return () => clearTimeout(timer);
  }, [error]);

  if (!error || !visible) {
    return null;
  }
  return (
    <div
      role="alert"
      className="fixed bottom-4 left-1/2 z-30 -translate-x-1/2 rounded-md bg-red-800 px-4 py-2 text-sm text-red-100 shadow-lg"
    >
      {error.message}
    </div>
  );
}

/**
 * The single-player AI game screen. Owns the `/ws/game` connection and the local
 * display mirror, and composes the board, side panel, promotion dialog, and
 * game-over overlay. Mounted only while an AI game is active, so its hooks are
 * never called conditionally.
 *
 * @param props.difficulty - The selected AI tier (drives the backend search budget).
 * @param props.humanColor - The side the human plays (board orientation default).
 * @param props.onExit - Returns to mode select (also drops the socket on unmount).
 */
function AiGameScreen({
  difficulty,
  humanColor,
  onExit,
}: {
  difficulty: Difficulty;
  humanColor: Color;
  onExit: () => void;
}) {
  const ws = useGameWebSocket({ difficulty, humanColor });
  const mirror = useGameState();
  // The mirror helpers are each `useCallback([])` in the hook, so destructuring
  // gives stable identities safe to list in effect / memo dependency arrays
  // (the mirror OBJECT itself is recreated every render and must not be a dep).
  const { syncFromFen, legalTargets, isPromotion, kingSquare } = mirror;
  // The hook's callbacks are stable; `state`, `error`, and `connected` are reactive.
  const { state, error, connected, sendMove, resign, newGame } = ws;
  // Typed composition-boundary locals forwarded to the panel / overlay.
  const aiThinking: AiThinkingMessage | null = ws.aiThinking;
  const gameOver: GameOverMessage | null = ws.gameOver;

  const boardWidth = useBoardWidth();
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [promotion, setPromotion] = useState<{ from: string; to: string } | null>(null);
  const [orientation, setOrientation] = useState<Color>(humanColor);

  // C1: the rendered position is always the server's authoritative FEN. Sync the
  // display-only mirror whenever a new state arrives so legal-target highlighting
  // and king-square lookups reflect the true position.
  useEffect(() => {
    if (state) {
      syncFromFen(state.fen);
    }
  }, [state, syncFromFen]);

  // It is the human's move only on their turn in an active game.
  const isHumanTurn = state?.status === 'active' && state.turn === humanColor;

  // Submit a move (drag-drop or the second click of click-to-move). Returns the
  // accept/reject boolean GameBoard forwards to react-chessboard: `false` snaps
  // the piece back, which is also how a promotion is deferred until chosen (C16:
  // the move travels over the WebSocket via `sendMove`).
  const submitMove = useCallback(
    (from: string, to: string): boolean => {
      if (!state || state.status !== 'active' || state.turn !== humanColor) {
        return false;
      }
      if (isPromotion(from, to)) {
        setPromotion({ from, to });
        setSelectedSquare(null);
        return false;
      }
      sendMove(from, to);
      setSelectedSquare(null);
      return true;
    },
    [state, humanColor, isPromotion, sendMove],
  );

  // Click-to-move: first click selects a piece that has legal targets; the second
  // click either plays a legal target, deselects, or re-selects another piece.
  const handleSquareClick = useCallback(
    (square: string): void => {
      if (!state || state.status !== 'active' || state.turn !== humanColor) {
        return;
      }
      if (selectedSquare === null) {
        if (legalTargets(square).length > 0) {
          setSelectedSquare(square);
        }
        return;
      }
      if (square === selectedSquare) {
        setSelectedSquare(null);
        return;
      }
      if (legalTargets(selectedSquare).includes(square)) {
        submitMove(selectedSquare, square);
        return;
      }
      setSelectedSquare(legalTargets(square).length > 0 ? square : null);
    },
    [state, humanColor, selectedSquare, legalTargets, submitMove],
  );

  const handleFlip = useCallback((): void => {
    setOrientation((current) => (current === 'white' ? 'black' : 'white'));
  }, []);

  // Start a fresh game: clear local selection/promotion, then reopen the socket
  // (the server treats a new connection as a new game).
  const handleNewGame = useCallback((): void => {
    setSelectedSquare(null);
    setPromotion(null);
    newGame();
  }, [newGame]);

  const legalForSelected = selectedSquare ? legalTargets(selectedSquare) : [];
  const checkSquare = state?.in_check ? kingSquare(state.turn) : null;

  return (
    <div className="flex min-h-screen w-full flex-col items-center justify-center gap-6 p-4 xl:flex-row xl:items-start xl:justify-center">
      <BackButton onClick={onExit} />
      <div className="flex w-full max-w-board flex-col gap-3">
        <GameBoard
          fen={state?.fen ?? ''}
          orientation={orientation}
          onMove={submitMove}
          onSquareClick={handleSquareClick}
          legalTargets={legalForSelected}
          lastMove={toBoardLastMove(state)}
          checkSquare={checkSquare}
          draggable={isHumanTurn}
          boardWidth={boardWidth}
        />
      </div>
      <SidePanel
        status={deriveStatus(state, connected, humanColor, 'AI')}
        turn={state?.turn ?? humanColor}
        moveHistory={state?.move_history ?? []}
        aiThinking={aiThinking}
        fen={state?.fen ?? 'start'}
        onResign={resign}
        onFlip={handleFlip}
        onNewGame={handleNewGame}
        connected={connected}
      />
      <PromotionDialog
        open={promotion !== null}
        color={humanColor}
        onSelect={(piece) => {
          if (promotion) {
            sendMove(promotion.from, promotion.to, piece);
            setPromotion(null);
          }
        }}
        onCancel={() => setPromotion(null)}
      />
      <GameOverOverlay gameOver={gameOver} onNewGame={handleNewGame} onExit={onExit} />
      <ErrorToast error={error} />
    </div>
  );
}

/**
 * The online (multiplayer) screen. Owns the single `/ws/multiplayer` connection
 * for the whole online session and switches between the lobby and the live game
 * WITHOUT tearing the socket down. Every hook runs unconditionally at the top;
 * the lobby-vs-game choice is made only afterward in the returned JSX, so the
 * Rules of Hooks hold across both phases.
 *
 * @param props.onExit - Returns to mode select (also drops the socket on unmount).
 */
function OnlineScreen({ onExit }: { onExit: () => void }) {
  const mp = useMultiplayerWebSocket();
  const mirror = useGameState();
  const { syncFromFen, legalTargets, isPromotion, kingSquare } = mirror;
  const { state, room, error, connected, createRoom, joinRoom, sendMove, resign } = mp;
  const gameOver: GameOverMessage | null = mp.gameOver;

  const boardWidth = useBoardWidth();
  const [selectedSquare, setSelectedSquare] = useState<string | null>(null);
  const [promotion, setPromotion] = useState<{ from: string; to: string } | null>(null);
  const [flipped, setFlipped] = useState(false);

  // C1: sync the display-only mirror from the authoritative server FEN.
  useEffect(() => {
    if (state) {
      syncFromFen(state.fen);
    }
  }, [state, syncFromFen]);

  // The player's seat; defaults to White until the server assigns this client a room.
  const myColor: Color = room?.color ?? 'white';
  const orientation: Color = flipped ? (myColor === 'white' ? 'black' : 'white') : myColor;
  const isMyTurn = state?.status === 'active' && state.turn === myColor;

  // Submit a move over the multiplayer socket (C16). Identical shape to the AI
  // screen, but gated on THIS player's seat and turn; promotions are deferred.
  const submitMove = useCallback(
    (from: string, to: string): boolean => {
      if (!state || state.status !== 'active' || state.turn !== myColor) {
        return false;
      }
      if (isPromotion(from, to)) {
        setPromotion({ from, to });
        setSelectedSquare(null);
        return false;
      }
      sendMove(from, to);
      setSelectedSquare(null);
      return true;
    },
    [state, myColor, isPromotion, sendMove],
  );

  const handleSquareClick = useCallback(
    (square: string): void => {
      if (!state || state.status !== 'active' || state.turn !== myColor) {
        return;
      }
      if (selectedSquare === null) {
        if (legalTargets(square).length > 0) {
          setSelectedSquare(square);
        }
        return;
      }
      if (square === selectedSquare) {
        setSelectedSquare(null);
        return;
      }
      if (legalTargets(selectedSquare).includes(square)) {
        submitMove(selectedSquare, square);
        return;
      }
      setSelectedSquare(legalTargets(square).length > 0 ? square : null);
    },
    [state, myColor, selectedSquare, legalTargets, submitMove],
  );

  const handleFlip = useCallback((): void => {
    setFlipped((current) => !current);
  }, []);

  // Lobby phase: shown until the server reports an active/finished position for
  // our room. The explicit null/status checks (rather than a derived boolean)
  // also narrow `room` and `state` to non-null for the game phase below.
  if (room === null || state === null || state.status === 'waiting') {
    return (
      <>
        <BackButton onClick={onExit} />
        <OnlineLobby
          onCreateRoom={createRoom}
          onJoinRoom={joinRoom}
          room={room}
          error={error}
          waiting={room !== null}
        />
        <ErrorToast error={error} />
      </>
    );
  }

  // Game phase: `room` and `state` are non-null here.
  const legalForSelected = selectedSquare ? legalTargets(selectedSquare) : [];
  const checkSquare = state.in_check ? kingSquare(state.turn) : null;

  return (
    <div className="flex min-h-screen w-full flex-col items-center justify-center gap-6 p-4 xl:flex-row xl:items-start xl:justify-center">
      <BackButton onClick={onExit} />
      <div className="flex w-full max-w-board flex-col gap-3">
        <GameBoard
          fen={state.fen}
          orientation={orientation}
          onMove={submitMove}
          onSquareClick={handleSquareClick}
          legalTargets={legalForSelected}
          lastMove={toBoardLastMove(state)}
          checkSquare={checkSquare}
          draggable={isMyTurn}
          boardWidth={boardWidth}
        />
      </div>
      <SidePanel
        status={deriveStatus(state, connected, myColor, 'Opponent')}
        turn={state.turn}
        moveHistory={state.move_history}
        fen={state.fen}
        onResign={resign}
        onFlip={handleFlip}
        onNewGame={onExit}
        connected={connected}
        roomCode={room.code}
      />
      <PromotionDialog
        open={promotion !== null}
        color={myColor}
        onSelect={(piece) => {
          if (promotion) {
            sendMove(promotion.from, promotion.to, piece);
            setPromotion(null);
          }
        }}
        onCancel={() => setPromotion(null)}
      />
      <GameOverOverlay gameOver={gameOver} onNewGame={onExit} onExit={onExit} />
      <ErrorToast error={error} />
    </div>
  );
}

/**
 * The routable screens, as a discriminated union. `ai-game` carries the chosen
 * difficulty and the human's color; `online` and `self-play` need no params
 * because their screens own the rest of their state.
 */
type View =
  | { name: 'mode-select' }
  | { name: 'ai-game'; difficulty: Difficulty; humanColor: Color }
  | { name: 'online' }
  | { name: 'self-play' };

/**
 * App — the SPA root. A tiny `useState` state machine routes between the five
 * screens without a router library; the lobby and multiplayer game share one
 * screen ({@link OnlineScreen}) so the multiplayer socket survives the
 * lobby → game transition. This is the file's only export (default), satisfying
 * the `App` export contract and `react-refresh/only-export-components`.
 *
 * @returns The currently routed screen.
 */
export default function App() {
  const [view, setView] = useState<View>({ name: 'mode-select' });

  // Navigation handlers, memoized so the screens they are passed to do not
  // re-render purely because App re-rendered.
  const goModeSelect = useCallback((): void => setView({ name: 'mode-select' }), []);
  const startAiGame = useCallback(
    (difficulty: Difficulty): void => setView({ name: 'ai-game', difficulty, humanColor: 'white' }),
    [],
  );
  const goOnline = useCallback((): void => setView({ name: 'online' }), []);
  const goSelfPlay = useCallback((): void => setView({ name: 'self-play' }), []);

  switch (view.name) {
    case 'mode-select':
      return (
        <ModeSelect onSelectAi={startAiGame} onPlayOnline={goOnline} onWatchSelfPlay={goSelfPlay} />
      );
    case 'ai-game':
      return (
        <AiGameScreen
          difficulty={view.difficulty}
          humanColor={view.humanColor}
          onExit={goModeSelect}
        />
      );
    case 'online':
      return <OnlineScreen onExit={goModeSelect} />;
    case 'self-play':
      return <SelfPlayView onExit={goModeSelect} />;
  }

  // Unreachable: the switch above is exhaustive over every View variant. The
  // explicit return keeps the function total for the compiler.
  return null;
}

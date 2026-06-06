/**
 * SelfPlayView — the AI-vs-AI self-play demonstration screen (AAP §0.5.3).
 *
 * The fifth and final routed screen of the Blitzy Chess SPA. It shows the
 * chessboard beside a live commentary panel and advances at no less than five
 * seconds per move, presenting the hand-built engine playing itself
 * (Hard-vs-Medium). `../App.tsx` routes to it as `<SelfPlayView onExit={…} />`
 * and the only control on the screen returns the player to the mode-select menu.
 *
 * Data source — a `window` hook, NOT a WebSocket (load-bearing). Unlike the AI
 * and multiplayer games, the self-play feed does NOT travel over a socket. The
 * backend self-play runner (`backend/chess_ai/self_play/runner.py`) computes the
 * whole game locally in Python and drives the real browser through Playwright:
 *
 *   1. It polls for readiness — `window.__BLITZY_SELF_PLAY__.ready === true` —
 *      which this component sets once it has mounted.
 *   2. It pushes each move by calling `window.__BLITZY_SELF_PLAY__.render(state)`
 *      via `page.evaluate(...)`, once per move.
 *
 * This component therefore INSTALLS that global hook on mount and translates
 * every `render(state)` call into React state, then renders the board and the
 * commentary from that state. There is no `/ws/self-play` endpoint and this file
 * opens no socket. The hook is also what makes the screen deterministic and
 * unit-testable: a test mounts `<SelfPlayView />` and calls
 * `window.__BLITZY_SELF_PLAY__.render({ … })` to assert the UI updates.
 *
 * The contract is best-effort by the runner's design: a missing or not-yet-ready
 * hook never aborts the Python-side game, the screen recording, or the
 * transcript. The push payload ({@link SelfPlayState}) is a LOCAL shape specific
 * to this driver — it is NOT one of the `../types` WebSocket wire messages — so
 * it is declared here rather than imported.
 *
 * Display-only and non-interactive (constraints C1 / C15). Like every screen in
 * the SPA, this is a pure view: it decides no legality and generates no moves.
 * The board renders ONLY through {@link GameBoard} (the single react-chessboard
 * wrapper, constraint C15 — no hand-rolled canvas or SVG) and is mounted
 * read-only — pieces are not draggable and any drop is rejected — because a
 * self-play demo accepts no human input.
 *
 * Build context. The project uses the JSX automatic runtime, so React is not
 * imported as a namespace; only the named hooks are. Styling uses the Tailwind 3
 * design tokens from `tailwind.config.js` and the helpers in
 * `../styles/index.css` — the `bg-panel` (`#1e1e1e`) dark commentary surface, the
 * `.board-cap` 640px board cap, the `.move-history-scroll` slim scroll region,
 * and the muted `text-secondary` (`#9ca3af`) text.
 *
 * @module components/SelfPlayView
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { GameBoard } from './GameBoard';

/**
 * One self-play state payload pushed by the backend runner via
 * `window.__BLITZY_SELF_PLAY__.render(state)`, once per move.
 *
 * This is the runner's LOCAL driver contract, not a `../types` wire message, so
 * it is declared here. Every field except `fen` is optional because the runner
 * is the sole producer and may omit telemetry; the component degrades
 * gracefully when a field is absent.
 */
interface SelfPlayState {
  /** The position AFTER the move, as a FEN string. */
  fen: string;
  /**
   * The squares of the move just played. The runner sends an object (it also
   * carries a `uci` field, which is ignored here) or a UCI string such as
   * `'e2e4'`; `null`/omitted before the first move.
   */
  lastMove?: { from: string; to: string } | string | null;
  /** Full-move number for the commentary line, e.g. `1`, `2`, `3`. */
  moveNumber?: number;
  /** Side to move in the new position (`'white'` / `'black'`). */
  sideToMove?: string;
  /** The move just played, in Standard Algebraic Notation (e.g. `'Nf3'`). */
  san?: string;
  /** Difficulty tier playing White (e.g. `'Hard'`). */
  whiteTier?: string;
  /** Difficulty tier playing Black (e.g. `'Medium'`). */
  blackTier?: string;
  /** Static evaluation in centipawns from White's point of view (positive = White better). */
  evalCp?: number;
  /** Coarse game status (`'playing'`, `'check'`, `'checkmate'`, `'gameover'`). */
  status?: string;
}

/**
 * One rendered line in the commentary list. `id` is a stable, monotonic key
 * (the SAN alone is not unique across a game), and the rest mirror the pushed
 * {@link SelfPlayState} fields the line displays.
 */
interface CommentaryEntry {
  /** Stable unique key for React, from the module-local counter. */
  id: number;
  /** Full-move number, when supplied. */
  moveNumber?: number;
  /** The move in SAN. */
  san?: string;
  /** White-POV centipawn evaluation, when supplied. */
  evalCp?: number;
}

/**
 * Global augmentation declaring the self-play render hook the Playwright runner
 * calls. Declared optional (`?`) so this component can both install it on mount
 * and `delete` it on unmount, and so strict TypeScript types every access
 * without `any`.
 */
declare global {
  interface Window {
    __BLITZY_SELF_PLAY__?: {
      /** `true` once the view is mounted and ready to receive `render` calls. */
      ready: boolean;
      /** Push one move's state into the view. */
      render: (state: SelfPlayState) => void;
      /** Optional reset back to the start position (used by tests / re-runs). */
      reset?: () => void;
    };
  }
}

/** The standard chess starting position, shown before the first move arrives. */
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

/**
 * Normalize the runner's `lastMove` into the `{ from, to }` shape {@link GameBoard}
 * highlights, or `null` when there is no move to show.
 *
 * Accepts the three forms the runner may send: a falsy value (`null`/`undefined`/
 * empty string) → `null`; a UCI string such as `'e2e4'` → the first two square
 * pairs (guarding against malformed strings shorter than four characters); or an
 * object, from which only `from`/`to` are kept (any extra `uci` field is
 * dropped).
 *
 * @param lastMove - The raw `lastMove` from a {@link SelfPlayState} push.
 * @returns The board-ready `{ from, to }` squares, or `null`.
 */
function normalizeLastMove(
  lastMove: SelfPlayState['lastMove'],
): { from: string; to: string } | null {
  if (!lastMove) {
    return null;
  }
  if (typeof lastMove === 'string') {
    if (lastMove.length < 4) {
      return null;
    }
    return { from: lastMove.slice(0, 2), to: lastMove.slice(2, 4) };
  }
  return { from: lastMove.from, to: lastMove.to };
}

/**
 * Format a White-POV centipawn score as a signed pawn value for a commentary
 * line: `30` → `'+0.30'`, `-150` → `'-1.50'`, `0` → `'0.00'`. The leading `+` is
 * added only for strictly positive scores; negatives carry their own sign and
 * zero is unsigned, so every value direction renders distinctly.
 *
 * @param cp - Score in centipawns from White's point of view.
 * @returns The formatted pawn-unit score.
 */
function formatEval(cp: number): string {
  const sign = cp > 0 ? '+' : '';
  return `${sign}${(cp / 100).toFixed(2)}`;
}

/**
 * Props for {@link SelfPlayView}. The parent (`App.tsx`) owns navigation, so the
 * screen is fully controlled through this single callback.
 */
interface SelfPlayViewProps {
  /** Leave the demonstration and return to the mode-select menu. */
  onExit: () => void;
}

/**
 * Render the self-play demonstration screen.
 *
 * Installs the `window.__BLITZY_SELF_PLAY__` hook on mount (and removes it on
 * unmount), turning each runner-pushed {@link SelfPlayState} into board and
 * commentary updates. See the module documentation for the full data-source
 * contract.
 *
 * @param props - See {@link SelfPlayViewProps}.
 * @returns The self-play screen element.
 */
export function SelfPlayView({ onExit }: SelfPlayViewProps) {
  // Board + feed state, all driven by the runner's render(state) pushes.
  const [fen, setFen] = useState<string>(START_FEN);
  const [lastMove, setLastMove] = useState<{ from: string; to: string } | null>(null);
  const [entries, setEntries] = useState<CommentaryEntry[]>([]);
  const [tiers, setTiers] = useState<{ white?: string; black?: string }>({});
  const [started, setStarted] = useState(false);

  // Monotonic source of stable commentary keys (SAN alone is not unique).
  const counterRef = useRef(0);
  // The scrollable commentary viewport, driven imperatively to auto-scroll.
  const logRef = useRef<HTMLDivElement | null>(null);

  // Translate one pushed state payload into React state. Memoized so the install
  // effect below has a stable dependency and does not reinstall the hook on
  // every render; every updater/ref it closes over is stable, so its deps are [].
  const handleRender = useCallback((state: SelfPlayState) => {
    if (state.fen) {
      setFen(state.fen);
    }
    setLastMove(normalizeLastMove(state.lastMove));
    setStarted(true);

    // Merge tier labels as they arrive, keeping any previously known side.
    if (state.whiteTier || state.blackTier) {
      setTiers((prev) => ({
        white: state.whiteTier ?? prev.white,
        black: state.blackTier ?? prev.black,
      }));
    }

    // A move with SAN appends a commentary line; positionless pushes do not.
    if (state.san) {
      counterRef.current += 1;
      const entry: CommentaryEntry = {
        id: counterRef.current,
        moveNumber: state.moveNumber,
        san: state.san,
        evalCp: state.evalCp,
      };
      setEntries((prev) => [...prev, entry]);
    }
  }, []);

  // Install the global render hook on mount; remove it on unmount. The runner
  // polls `ready === true`, then calls `render(state)` once per move. Under
  // React 18 StrictMode this effect runs mount → unmount → mount; the cleanup
  // removes the hook and the next mount reinstalls it, so `ready` ends up true.
  useEffect(() => {
    window.__BLITZY_SELF_PLAY__ = {
      ready: true,
      render: handleRender,
      reset: () => {
        setFen(START_FEN);
        setLastMove(null);
        setEntries([]);
        setTiers({});
        setStarted(false);
        counterRef.current = 0;
      },
    };
    return () => {
      // Valid because the property is optional on the augmented Window type.
      delete window.__BLITZY_SELF_PLAY__;
    };
  }, [handleRender]);

  // Keep the newest commentary line in view as the game streams in.
  useEffect(() => {
    const el = logRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [entries]);

  // Show the matchup subtitle only once both tiers are known (the runner sends
  // them together on the first push).
  const matchupKnown = tiers.white != null && tiers.black != null;

  return (
    <div className="flex min-h-screen w-full flex-col items-center gap-4 p-4">
      <header className="flex w-full max-w-5xl items-center justify-between gap-4">
        <h1 className="text-xl font-semibold text-gray-100">Self-Play Demonstration</h1>
        <button
          type="button"
          onClick={onExit}
          className="flex min-h-11 items-center justify-center rounded bg-gray-700 px-4 py-2 font-medium text-gray-100 hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 motion-safe:transition-colors"
        >
          Exit to menu
        </button>
      </header>

      <div className="flex w-full max-w-5xl flex-col gap-4 md:flex-row">
        {/* Board — rendered ONLY through GameBoard (C15), mounted read-only. */}
        <div className="board-cap mx-auto w-full">
          <GameBoard
            fen={fen}
            orientation="white"
            onMove={() => false}
            lastMove={lastMove}
            draggable={false}
            id="self-play-board"
          />
        </div>

        {/* Live commentary panel. */}
        <aside className="flex w-full flex-col rounded-lg bg-panel p-4 md:max-w-sm">
          <h2 className="mb-2 text-lg font-semibold text-gray-100">
            Commentary
            {matchupKnown && (
              <span className="ml-2 text-sm font-normal text-secondary">
                {tiers.white} (White) vs {tiers.black} (Black)
              </span>
            )}
          </h2>

          <div
            ref={logRef}
            aria-live="polite"
            className="move-history-scroll max-h-[480px] flex-1 rounded bg-panel-inset p-2"
          >
            {entries.length === 0 ? (
              <p className="text-sm text-secondary">
                {started ? 'Playing…' : 'Waiting for the self-play feed…'}
              </p>
            ) : (
              <ol aria-label="Self-play commentary" className="space-y-1 text-sm">
                {entries.map((entry) => (
                  <li key={entry.id} className="flex items-baseline gap-2">
                    {entry.moveNumber != null && (
                      <span className="shrink-0 text-secondary">{entry.moveNumber}.</span>
                    )}
                    <span className="font-medium text-gray-100">{entry.san}</span>
                    {entry.evalCp != null && (
                      <span className="ml-auto shrink-0 text-secondary">
                        {formatEval(entry.evalCp)}
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

export default SelfPlayView;

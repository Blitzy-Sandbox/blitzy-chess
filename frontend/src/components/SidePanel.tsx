/**
 * SidePanel — the dark game side panel for the Blitzy Chess SPA.
 *
 * A presentational panel rendered beside the chessboard on BOTH game screens —
 * the single-player AI game and the real-time multiplayer game — and composed by
 * `../App.tsx`. It gathers everything that sits next to the board into one
 * self-contained unit:
 *
 *   - the human-readable game status and whose turn it is;
 *   - an optional WebSocket connection indicator and, for multiplayer, the
 *     six-character room code;
 *   - the AI search telemetry ("AI thinking…"), shown only while the engine is
 *     searching in an AI game (multiplayer games never set it);
 *   - the paired-algebraic move history ({@link MoveHistory});
 *   - the captured pieces and material differential ({@link CapturedPieces});
 *   - the three game controls: New game, Flip board, and Resign.
 *
 * Composition (load-bearing). The move history and captured-pieces summaries are
 * CHILDREN of this panel — `../App.tsx` renders `<SidePanel>` and never mounts
 * {@link MoveHistory} or {@link CapturedPieces} directly. This keeps the side
 * panel a single component the App can drop next to the board.
 *
 * Display-only (constraint C1). Like every component in the SPA, this panel is a
 * pure view over data the python-chess backend has already validated. It holds no
 * game state, decides no legality, and only forwards user intent (resign / flip /
 * new game) to the callbacks the parent supplies.
 *
 * Build context. The project uses the JSX automatic runtime, so React is not
 * imported; this module needs no hooks (it is purely presentational), and the
 * `<>…</>` fragment is available without importing React. Styling uses the
 * Tailwind 3 design tokens from `tailwind.config.js` — notably the `bg-panel`
 * dark surface (`#1e1e1e`) and the muted `text-secondary` (`#9ca3af`) — over the
 * global `--text-primary` (`#e8e8e8`) body color.
 *
 * @module components/SidePanel
 */
import type { AiThinkingMessage, Color } from '../types';

import { CapturedPieces, type CapturedMove } from './CapturedPieces';
import { MoveHistory } from './MoveHistory';

/**
 * Props for {@link SidePanel}. This shape is shared with `../App.tsx`; keep the
 * two in sync.
 */
interface SidePanelProps {
  /** Human-readable status line, e.g. "Your move", "AI is thinking", "Checkmate". */
  status: string;
  /** Whose turn it is in the authoritative position. */
  turn: Color;
  /**
   * Moves in play order as SAN strings (`['e4', 'e5', 'Nf3', …]`), forwarded to
   * {@link MoveHistory}, which pairs them into numbered full-move rows.
   */
  moveHistory: string[];
  /**
   * Live AI search telemetry for the thinking indicator, present ONLY in AI
   * games while the engine searches. `null`/omitted — and always so in
   * multiplayer — hides the indicator entirely. `evaluation` is centipawns from
   * White's point of view (positive = White better) and is rendered verbatim
   * (never re-negated).
   */
  aiThinking?: AiThinkingMessage | null;
  /**
   * Authoritative capture history, forwarded to {@link CapturedPieces}
   * (`<CapturedPieces moves={capturedMoves} />`). Each entry names the side that
   * moved and the piece it captured; the child tallies the captured-piece rows
   * and the material differential from it. Captures are MOVE-driven rather than
   * derived from the FEN because counting "missing" pieces from a position is
   * unsound across promotions. Omitted renders the start-of-game empty state.
   */
  capturedMoves?: ReadonlyArray<CapturedMove>;
  /**
   * Current FEN of the authoritative position. Part of the position contract
   * shared with `../App.tsx` and accepted here for that contract's stability.
   * The captured-pieces summary is driven by {@link SidePanelProps.capturedMoves}
   * (the {@link CapturedPieces} dependency is move-driven), so the panel does not
   * itself read the FEN.
   */
  fen?: string;
  /** Resign the current game. */
  onResign: () => void;
  /** Flip the board orientation. */
  onFlip: () => void;
  /** Abandon the current game and start a fresh one. */
  onNewGame: () => void;
  /**
   * Optional WebSocket connection indicator. When provided, a small colored dot
   * reflects the live/lost socket; omitted hides the dot.
   */
  connected?: boolean;
  /** Six-character room code, shown for multiplayer games only. */
  roomCode?: string;
}

/**
 * Format a White-POV centipawn score for display, showing BOTH pawn units and
 * the raw centipawns: `120` → `"+1.20 (+120cp)"`, `-50` → `"-0.50 (-50cp)"`,
 * `0` → `"0.00 (0cp)"`. The leading `+` is added only for strictly positive
 * scores; negative scores carry their own sign and zero is unsigned.
 *
 * @param cp - Score in centipawns from White's point of view.
 * @returns The formatted score string.
 */
function formatEval(cp: number): string {
  const sign = cp > 0 ? '+' : '';
  return `${sign}${(cp / 100).toFixed(2)} (${sign}${cp}cp)`;
}

/**
 * Render the dark game side panel.
 *
 * See {@link SidePanelProps} for the full contract. The AI-thinking block renders
 * only when `aiThinking` is truthy; the connection dot only when `connected` is
 * defined; and the room code only when `roomCode` is set.
 *
 * @param props - See {@link SidePanelProps}.
 * @returns The side-panel element.
 */
export function SidePanel({
  status,
  turn,
  moveHistory,
  aiThinking,
  capturedMoves,
  onResign,
  onFlip,
  onNewGame,
  connected,
  roomCode,
}: SidePanelProps) {
  return (
    <aside className="flex w-full max-w-sm flex-col gap-4 rounded-lg bg-panel p-4 text-sm">
      <header className="flex items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{status}</h2>
        {connected !== undefined && (
          <span
            role="img"
            aria-label={connected ? 'Connected' : 'Disconnected'}
            title={connected ? 'Connected' : 'Disconnected'}
            className={`h-2.5 w-2.5 shrink-0 rounded-full ${
              connected ? 'bg-green-500' : 'bg-red-500'
            }`}
          />
        )}
      </header>

      <div className="flex items-center justify-between gap-2 text-secondary">
        <span>
          Turn: <span className="font-medium capitalize text-gray-100">{turn}</span>
        </span>
        {roomCode && (
          <span className="font-mono">
            Room: <span className="font-semibold tracking-widest text-gray-100">{roomCode}</span>
          </span>
        )}
      </div>

      {aiThinking && (
        <div className="rounded-md bg-black/30 p-3" aria-live="polite">
          <div className="mb-1 flex items-center gap-2 font-medium text-gray-200">
            <span
              aria-hidden="true"
              className="inline-block h-2 w-2 animate-pulse rounded-full bg-yellow-400"
            />
            AI thinking…
          </div>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-secondary">
            <dt>Depth</dt>
            <dd className="text-right text-gray-200">{aiThinking.depth}</dd>
            <dt>Eval</dt>
            <dd className="text-right text-gray-200">{formatEval(aiThinking.evaluation)}</dd>
            <dt>Nodes</dt>
            <dd className="text-right text-gray-200">{aiThinking.nodes.toLocaleString()}</dd>
            {aiThinking.mate_in != null && (
              <>
                <dt>Mate</dt>
                <dd className="text-right text-gray-200">in {Math.abs(aiThinking.mate_in)}</dd>
              </>
            )}
          </dl>
          {aiThinking.pv.length > 0 && (
            <p className="mt-1 truncate text-secondary" title={aiThinking.pv.join(' ')}>
              PV: <span className="text-gray-200">{aiThinking.pv.join(' ')}</span>
            </p>
          )}
        </div>
      )}

      <MoveHistory moves={moveHistory} />
      <CapturedPieces moves={capturedMoves} />

      <div className="mt-auto flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onNewGame}
          className="flex min-h-11 flex-1 items-center justify-center rounded bg-green-700 px-3 py-2 font-medium text-gray-100 hover:bg-green-600"
        >
          New game
        </button>
        <button
          type="button"
          onClick={onFlip}
          className="flex min-h-11 flex-1 items-center justify-center rounded bg-gray-700 px-3 py-2 font-medium text-gray-100 hover:bg-gray-600"
        >
          Flip board
        </button>
        <button
          type="button"
          onClick={onResign}
          className="flex min-h-11 flex-1 items-center justify-center rounded bg-red-800 px-3 py-2 font-medium text-gray-100 hover:bg-red-700"
        >
          Resign
        </button>
      </div>
    </aside>
  );
}

export default SidePanel;

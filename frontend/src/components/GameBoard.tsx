/**
 * GameBoard — the single chessboard renderer for the whole SPA (constraint C15).
 *
 * A thin, presentational wrapper around react-chessboard (pinned to the 4.x
 * line, individual-prop API). EVERY screen that shows a board — the AI game, the
 * multiplayer game, and the self-play demonstration — renders the position
 * through THIS component, and the board is drawn ONLY by react-chessboard. There
 * is no hand-rolled `<canvas>` or `<svg>` board anywhere in the application; that
 * is the load-bearing constraint C15.
 *
 * Server-authoritative, display-only (constraints C1 / C16). This component
 * never decides legality, never generates moves, and never mutates game state.
 * The parent supplies the authoritative `fen` (validated by python-chess on the
 * backend), the set of `legalTargets` to highlight, the `lastMove` squares, and
 * the `checkSquare`; this component only visualizes them and forwards user
 * intent (a drag-drop or a click) back to the parent via `onMove` /
 * `onSquareClick`. The boolean returned by `onMove` flows straight back to
 * react-chessboard's `onPieceDrop`, which uses it to keep or snap-back the
 * dragged piece — this is also how the App defers a promotion (by returning
 * `false` until the promotion piece is chosen).
 *
 * Interaction model. Both input styles work: drag-and-drop (react-chessboard's
 * `onPieceDrop`) and click-to-move (`onSquareClick`, where the parent tracks the
 * first/second click). The board flips on demand via `orientation`, caps its
 * width at the design's 640px, and paints three highlight layers through
 * react-chessboard's `customSquareStyles`:
 *   - the last move (both source and destination squares),
 *   - the legal-move targets (a lichess-style dot drawn as a radial gradient),
 *   - the king square when in check (a translucent red wash).
 *
 * Styling. The board palette is the lichess style, applied through the shared
 * `--board-light` / `--board-dark` CSS variable tokens (defined in
 * `../styles/index.css`, mirrored by Tailwind's `board-light` / `board-dark`)
 * rather than duplicated hex literals — passed inline because react-chessboard's
 * square-style props accept style objects, not class names. The highlight values mirror the
 * `.square-*` helpers in `../styles/index.css`; they are inlined here because
 * react-chessboard styles individual squares through `customSquareStyles`
 * (`CSSProperties` objects), not via class names. The width cap uses the
 * `.board-cap` helper from that same stylesheet.
 *
 * Build context. The project uses the JSX automatic runtime, so React is not
 * imported as a namespace; only the `useMemo` hook and the `CSSProperties` type
 * are imported from `react`. The decision to pin react-chessboard to the 4.x
 * individual-prop API (rather than the 5.x `options` object) is recorded in
 * docs/decision-log.md, per the Explainability rule, not here.
 *
 * @module components/GameBoard
 */
import { useMemo, type CSSProperties } from 'react';

import { Chessboard } from 'react-chessboard';

import type { Color } from '../types';

/**
 * Props for {@link GameBoard}.
 *
 * The squares are lowercase algebraic strings (e.g. `'e2'`, `'g8'`). Note that
 * `lastMove` here is the **camelCase** `{ from, to }` shape: the wire protocol
 * uses snake_case (`{ from_square, to_square }`), and `../App.tsx` performs that
 * mapping before passing the value down — this component only ever sees
 * `{ from, to }`.
 */
interface GameBoardProps {
  /**
   * The authoritative position as a FEN string (validated server-side by
   * python-chess). An empty string is treated as the standard starting
   * position.
   */
  fen: string;
  /** Board orientation; the named color is placed at the bottom of the board. */
  orientation: Color;
  /**
   * Called when the user attempts a move by dragging a piece from `from` to
   * `to`. MUST return whether the move is accepted: react-chessboard keeps the
   * piece on `to` when `true` and snaps it back when `false`. Returning `false`
   * is also how the parent defers a pawn promotion until the piece is chosen.
   */
  onMove: (from: string, to: string) => boolean;
  /**
   * Optional click handler for click-to-move. The parent tracks the first
   * (select) and second (target) clicks; this component simply forwards the
   * clicked square.
   */
  onSquareClick?: (square: string) => void;
  /**
   * Squares to mark as legal move targets for the currently selected piece.
   * Each is highlighted with a lichess-style dot. Supplied by the parent; this
   * component does not compute legality (constraint C1).
   */
  legalTargets?: string[];
  /**
   * The source and destination squares of the most recent move, both
   * highlighted, or `null` before any move has been made.
   */
  lastMove?: { from: string; to: string } | null;
  /**
   * The square of the king that is currently in check, highlighted in red, or
   * `null` when no king is in check.
   */
  checkSquare?: string | null;
  /** Whether pieces can be dragged. Defaults to `true`. */
  draggable?: boolean;
  /** Desired board width in pixels. Capped at the design maximum of 640. */
  boardWidth?: number;
  /**
   * Board identifier, forwarded to react-chessboard. Required only when more
   * than one board is mounted at once (drag-and-drop disambiguation).
   */
  id?: string;
}

/** Last-move highlight (source and destination squares) — lichess green wash. */
const LAST_MOVE_STYLE: CSSProperties = { backgroundColor: 'rgba(155, 199, 0, 0.41)' };

/** Legal-move target marker — a lichess-style dot drawn as a radial gradient. */
const LEGAL_TARGET_STYLE: CSSProperties = {
  background: 'radial-gradient(circle, rgba(0, 0, 0, 0.24) 22%, transparent 24%)',
};

/** King-in-check highlight — a translucent red wash on the king's square. */
const CHECK_STYLE: CSSProperties = { backgroundColor: 'rgba(255, 0, 0, 0.45)' };

/**
 * Render the chessboard (constraint C15: react-chessboard only).
 *
 * Computes the per-square highlight styles from the parent-supplied
 * `legalTargets`, `lastMove`, and `checkSquare`, then renders react-chessboard
 * with the lichess palette and the 4.x individual-prop API. User intent is
 * forwarded to the parent: a drag-drop calls `onMove` (whose boolean controls
 * keep/snap-back), and a click calls `onSquareClick`.
 *
 * @param props - See {@link GameBoardProps}.
 * @returns The board element, width-capped and horizontally centered.
 */
export function GameBoard({
  fen,
  orientation,
  onMove,
  onSquareClick,
  legalTargets,
  lastMove,
  checkSquare,
  draggable,
  boardWidth,
  id,
}: GameBoardProps) {
  // Build the per-square highlight map. Layered so a single square can carry
  // more than one highlight: a legal target that is also the last-move square
  // keeps both the green wash and the legal-move dot. Memoized on the three
  // inputs so the object identity is stable between unrelated re-renders.
  const customSquareStyles = useMemo<Record<string, CSSProperties>>(() => {
    const styles: Record<string, CSSProperties> = {};

    // Last move: highlight both the source and destination squares.
    if (lastMove) {
      styles[lastMove.from] = { ...LAST_MOVE_STYLE };
      styles[lastMove.to] = { ...LAST_MOVE_STYLE };
    }

    // Legal targets: merge the dot onto any existing entry so it stacks with a
    // co-located last-move highlight rather than replacing it.
    for (const square of legalTargets ?? []) {
      styles[square] = { ...styles[square], ...LEGAL_TARGET_STYLE };
    }

    // King in check: merge the red wash onto any existing entry.
    if (checkSquare) {
      styles[checkSquare] = { ...styles[checkSquare], ...CHECK_STYLE };
    }

    return styles;
  }, [legalTargets, lastMove, checkSquare]);

  // Cap the board at the design maximum (640px); default to it when unset.
  const width = Math.min(boardWidth ?? 640, 640);

  return (
    <div className="board-cap mx-auto">
      <Chessboard
        id={id}
        position={fen === '' ? 'start' : fen}
        boardOrientation={orientation}
        boardWidth={width}
        arePiecesDraggable={draggable ?? true}
        onPieceDrop={(source, target) => onMove(source, target)}
        onSquareClick={onSquareClick ? (square) => onSquareClick(square) : undefined}
        customLightSquareStyle={{ backgroundColor: 'var(--board-light)' }}
        customDarkSquareStyle={{ backgroundColor: 'var(--board-dark)' }}
        customSquareStyles={customSquareStyles}
        customBoardStyle={{ borderRadius: '4px' }}
      />
    </div>
  );
}

export default GameBoard;

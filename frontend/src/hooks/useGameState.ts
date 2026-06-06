/**
 * useGameState — the local chess.js mirror for the React SPA's display + SAN
 * needs.
 *
 * This hook wraps a single chess.js `Chess` instance and exposes a small,
 * reactive view of the current position: the FEN, whose turn it is, whether the
 * side to move is in check, plus three stateless helpers the board uses to
 * highlight legal-move targets, detect a promotion, and locate a king.
 *
 * Authority (canonical constraint C1): this mirror is DISPLAY + SAN ONLY. It is
 * never the source of truth for legality — the python-chess backend, reached
 * over WebSocket, validates and applies every move and is authoritative. The
 * mirror is driven one-way FROM the server's `state.fen` through
 * {@link UseGameStateResult.syncFromFen}; it never blocks, rejects, or
 * "corrects" a move. An unparseable FEN leaves the mirror unchanged.
 *
 * Identity stability: the four helpers are wrapped in `useCallback([])`, so
 * their identities are stable across renders and safe to list in a consumer's
 * effect dependency array. The `Chess` instance lives in a ref (mutable,
 * non-reactive); only the derived `fen` / `turn` / `inCheck` values are React
 * state.
 *
 * This is the only hook that depends on chess.js. The rationale for these
 * choices is recorded in docs/decision-log.md, not in these comments, per the
 * Explainability rule.
 *
 * @module hooks/useGameState
 */

import { useCallback, useRef, useState } from 'react';
import { Chess } from 'chess.js';
import type { Square } from 'chess.js';
import type { Color } from '../types';

/** Standard chess starting position, used to seed the reactive state. */
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

/** Map chess.js's single-letter side (`'w'` / `'b'`) to the wire {@link Color}. */
function toColor(turn: 'w' | 'b'): Color {
  return turn === 'w' ? 'white' : 'black';
}

/**
 * The reactive view plus display helpers returned by {@link useGameState}.
 * Shared with `../App.tsx`; the shape is part of that contract and kept exact.
 */
export interface UseGameStateResult {
  /** The mirror's current FEN, reflecting the last successfully synced position. */
  fen: string;
  /** The side to move in the mirrored position. */
  turn: Color;
  /** Whether the side to move is in check, for board highlighting. */
  inCheck: boolean;
  /** Replace the mirror with the server's authoritative FEN (no-op if invalid). */
  syncFromFen(fen: string): void;
  /** Unique destination squares reachable from `square`, for target highlighting. */
  legalTargets(square: string): string[];
  /** Whether moving a pawn from `from` to `to` reaches the last rank. */
  isPromotion(from: string, to: string): boolean;
  /** The square hosting `color`'s king, or `null` if absent from the position. */
  kingSquare(color: Color): string | null;
}

/**
 * React hook exposing a chess.js-backed, display-only mirror of the position.
 *
 * @returns A {@link UseGameStateResult} with the reactive `fen` / `turn` /
 *   `inCheck` values and the stable `syncFromFen` / `legalTargets` /
 *   `isPromotion` / `kingSquare` helpers.
 */
export function useGameState(): UseGameStateResult {
  const chessRef = useRef<Chess>(new Chess());
  const [fen, setFen] = useState<string>(START_FEN);
  const [turn, setTurn] = useState<Color>('white');
  const [inCheck, setInCheck] = useState<boolean>(false);

  const syncFromFen = useCallback((nextFen: string): void => {
    const chess = chessRef.current;
    try {
      chess.load(nextFen);
    } catch {
      // Ignore an unparseable FEN and leave the mirror unchanged (C1 display-only).
      return;
    }
    setFen(chess.fen());
    setTurn(toColor(chess.turn()));
    setInCheck(chess.isCheck());
  }, []);

  const legalTargets = useCallback((square: string): string[] => {
    const moves = chessRef.current.moves({ square: square as Square, verbose: true });
    return Array.from(new Set(moves.map((move) => move.to)));
  }, []);

  const isPromotion = useCallback((from: string, to: string): boolean => {
    const piece = chessRef.current.get(from as Square);
    if (!piece || piece.type !== 'p') {
      return false;
    }
    const rank = to.charAt(1);
    return (piece.color === 'w' && rank === '8') || (piece.color === 'b' && rank === '1');
  }, []);

  const kingSquare = useCallback((color: Color): string | null => {
    const want = color === 'white' ? 'w' : 'b';
    for (const row of chessRef.current.board()) {
      for (const cell of row) {
        if (cell && cell.type === 'k' && cell.color === want) {
          return cell.square;
        }
      }
    }
    return null;
  }, []);

  return { fen, turn, inCheck, syncFromFen, legalTargets, isPromotion, kingSquare };
}

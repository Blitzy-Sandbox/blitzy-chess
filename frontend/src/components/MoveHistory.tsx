/**
 * MoveHistory — paired algebraic move list (constraint C7).
 *
 * A pure, display-only presentational component for the Blitzy Chess SPA. It
 * renders inside `SidePanel.tsx` (which passes `moves={moveHistory}`) and shows
 * the game's moves as PAIRED algebraic notation: one numbered row per full move,
 * `n. whiteSAN blackSAN`. This paired layout is the load-bearing constraint C7.
 *
 * Source of truth — the parent supplies `moves` as an ordered list of Standard
 * Algebraic Notation (SAN) strings in play order
 * (`[whiteMove1, blackMove1, whiteMove2, ...]`). That list mirrors the position
 * the server has already validated with python-chess; this component never
 * computes SAN and never decides legality (constraint C1). It only visualizes
 * what the authoritative backend has accepted.
 *
 * Behaviour:
 *   - Moves are paired into full-move rows. White's move at even index `i` and
 *     Black's reply at `i + 1` share row number `i / 2 + 1`. The black cell is
 *     omitted for a trailing white-only move (e.g. `3. Bb5`), so the list never
 *     renders `undefined`.
 *   - The scroll region auto-scrolls to the newest move whenever `moves`
 *     changes, keeping the latest play in view as the game grows.
 *
 * Rendering notes:
 *   - Semantic `<ol>` / `<li>` markup conveys the ordered move sequence to
 *     assistive technology; the list carries an accessible label.
 *   - The fixed-height, scrollable container uses the `.move-history-scroll`
 *     helper from `../styles/index.css` (slim dark scrollbar). That stylesheet
 *     is imported once globally in `main.tsx`, so this module references only
 *     the class name and imports no CSS itself.
 *
 * @module components/MoveHistory
 */
import { useEffect, useRef } from 'react';

/**
 * Props for {@link MoveHistory}.
 */
interface MoveHistoryProps {
  /**
   * Moves in play order as SAN strings:
   * `[whiteMove1, blackMove1, whiteMove2, ...]`. An empty list renders the
   * start-of-game placeholder.
   */
  moves: string[];
}

/**
 * A single full-move row: the move number, White's move, and — once it has been
 * played — Black's reply. `black` is absent for the final white-only move.
 */
interface MoveRow {
  /** Full-move number, starting at 1. */
  number: number;
  /** White's move in SAN. */
  white: string;
  /** Black's reply in SAN, or `undefined` when White has the last move. */
  black?: string;
}

/**
 * Render the paired-algebraic move history (constraint C7).
 *
 * Pairs the flat SAN list into numbered full-move rows and renders each as
 * `n. white black`, omitting the black cell for a trailing white-only move. The
 * region auto-scrolls to the latest move on every `moves` change. When `moves`
 * is empty, a muted "No moves yet." placeholder is shown instead of the list.
 *
 * @param props - See {@link MoveHistoryProps}.
 * @returns The move-history panel element.
 */
export function MoveHistory({ moves }: MoveHistoryProps) {
  // The scroll viewport; a ref lets the effect drive scrollTop imperatively.
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to the newest move whenever the move list changes. Keying the
  // effect on `moves` fires it on every new move (the array reference changes),
  // pinning the latest play to the bottom of the visible region.
  useEffect(() => {
    const container = containerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [moves]);

  // Pair the flat SAN list into full-move rows: white at even index `i`, black
  // at `i + 1` (absent for a trailing white-only move), numbered `i / 2 + 1`.
  const rows: MoveRow[] = [];
  for (let i = 0; i < moves.length; i += 2) {
    rows.push({ number: i / 2 + 1, white: moves[i], black: moves[i + 1] });
  }

  // BLITZY [A11Y]: the muted `text-gray-500` used for move numbers and the
  // empty-state placeholder computes below WCAG AA 4.5:1 for small secondary
  // text on the dark panel. It is kept per the design source (deliberate
  // secondary-text treatment) and flagged for designer review rather than
  // silently darkened.
  return (
    <div ref={containerRef} className="move-history-scroll max-h-[220px] rounded bg-black/20 p-2">
      {rows.length === 0 ? (
        <p className="text-sm text-gray-500">No moves yet.</p>
      ) : (
        <ol aria-label="Move history" className="text-sm">
          {rows.map((row) => (
            <li key={row.number} className="flex gap-2 py-0.5">
              <span className="w-7 shrink-0 text-right text-gray-500">{row.number}.</span>
              <span className="w-16 font-medium text-gray-100">{row.white}</span>
              {row.black && <span className="w-16 font-medium text-gray-100">{row.black}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export default MoveHistory;

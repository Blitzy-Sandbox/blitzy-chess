/**
 * CapturedPieces — captured pieces and material differential.
 *
 * A pure, display-only presentational component for the Blitzy Chess SPA. It
 * renders inside `SidePanel.tsx` and shows two rows:
 *
 *   - Row 1: the pieces White has captured (which are BLACK pieces, drawn with
 *     black glyphs), followed by a green `+N` badge when White leads on material.
 *   - Row 2: the pieces Black has captured (which are WHITE pieces, drawn with
 *     white glyphs), followed by a green `+N` badge when Black leads.
 *
 * Source of truth — the move history, NOT the FEN. Captures are tallied from the
 * moves played: each capturing move names the piece it removed (`captured`) and
 * the side that made it (`color`). Counting "missing" pieces from a FEN is
 * unsound across legal promotions — a promoted pawn would masquerade as a
 * captured pawn, and a promoted queen would hide a genuinely captured queen — so
 * this component never inspects the FEN. The move stream is the same one
 * python-chess validates on the server (constraint C1); this component only
 * visualizes what the authoritative server has already accepted.
 *
 * Rendering notes:
 *   - Pieces are real Unicode chess glyphs (no images, SVG, or canvas — C15).
 *   - Decorative glyphs and the numeric badge are `aria-hidden`; an equivalent
 *     visually-hidden (`sr-only`) summary conveys the same information to
 *     assistive technology, and the group carries an accessible label.
 *   - Rows wrap (`flex-wrap`) so a long capture list never overflows a narrow
 *     side panel, and an explicit placeholder marks a side with no captures so
 *     an empty row never reads as missing content.
 *
 * @module components/CapturedPieces
 */

/**
 * The five non-king piece types, keyed by their lowercase letter. Kings are
 * excluded: a king is never captured, so it never contributes to captures or to
 * the material differential.
 */
type PieceType = 'p' | 'n' | 'b' | 'r' | 'q';

/**
 * The capture-relevant fields of a single played move. This is a structural
 * subset of a chess.js verbose move (`{ color, captured, ... }`), so a caller
 * can pass `game.history({ verbose: true })` directly. Only `color` and
 * `captured` are read; all other move fields are ignored.
 */
export interface CapturedMove {
  /** Side that made the move: `'w'` (White) or `'b'` (Black). */
  color: 'w' | 'b';
  /**
   * Piece type captured by this move, if it was a capture. A promotion is not
   * itself a capture, so a promoting pawn never appears here; a capture during a
   * promotion still reports the captured piece. May be `undefined` for a quiet
   * move; only `p`/`n`/`b`/`r`/`q` are counted.
   */
  captured?: string;
}

/**
 * Props for {@link CapturedPieces}.
 */
interface CapturedPiecesProps {
  /**
   * Authoritative move history. Capturing moves drive both the captured-piece
   * rows and the material differential. Defaults to an empty list, which renders
   * the start-of-game empty state. Accepts a chess.js verbose history directly.
   */
  moves?: ReadonlyArray<CapturedMove>;
}

/**
 * Conventional material values (pawn 1 … queen 9) that drive the differential.
 * The king is valueless here by design.
 */
const VALUES: Record<PieceType, number> = { p: 1, n: 3, b: 3, r: 5, q: 9 };

/** White piece glyphs (rendered for the pieces Black has captured). */
const WHITE_GLYPHS: Record<PieceType, string> = { p: '♙', n: '♘', b: '♗', r: '♖', q: '♕' };

/** Black piece glyphs (rendered for the pieces White has captured). */
const BLACK_GLYPHS: Record<PieceType, string> = { p: '♟', n: '♞', b: '♝', r: '♜', q: '♛' };

/** Spoken piece names for the screen-reader summary. */
const PIECE_NAMES: Record<PieceType, string> = {
  q: 'queen',
  r: 'rook',
  b: 'bishop',
  n: 'knight',
  p: 'pawn',
};

/** Display order for captured pieces — strongest first (queen → pawn). */
const ORDER: PieceType[] = ['q', 'r', 'b', 'n', 'p'];

/**
 * Type guard narrowing an arbitrary `captured` value to a tracked
 * {@link PieceType}. Anything outside `p`/`n`/`b`/`r`/`q` (including `undefined`
 * and a stray `k`) is rejected, so the tally only ever counts real, capturable
 * pieces.
 *
 * @param value - The `captured` field of a move, or `undefined`.
 * @returns `true` when `value` is one of the five tracked piece letters.
 */
function isPieceType(value: string | undefined): value is PieceType {
  return value === 'p' || value === 'n' || value === 'b' || value === 'r' || value === 'q';
}

/**
 * Build a zeroed per-type tally.
 *
 * @returns A fresh `Record<PieceType, number>` with every count set to 0.
 */
function emptyCounts(): Record<PieceType, number> {
  return { p: 0, n: 0, b: 0, r: 0, q: 0 };
}

/**
 * Compose a screen-reader summary of one side's captures, e.g. "White has
 * captured 1 queen, 2 pawns" or "Black has captured no pieces".
 *
 * @param side - "White" or "Black".
 * @param counts - That side's per-type capture tally.
 * @returns A complete, human-readable sentence.
 */
function describeCaptures(side: 'White' | 'Black', counts: Record<PieceType, number>): string {
  const parts = ORDER.filter((type) => counts[type] > 0).map((type) => {
    const n = counts[type];
    return `${n} ${PIECE_NAMES[type]}${n === 1 ? '' : 's'}`;
  });
  return parts.length === 0
    ? `${side} has captured no pieces`
    : `${side} has captured ${parts.join(', ')}`;
}

/**
 * Render the captured-pieces summary and material differential for a game.
 *
 * Captures are tallied from the authoritative move history: each capturing move
 * adds the piece it removed to the moving side's tally. White's captures are the
 * Black pieces it has taken, and vice versa. The material differential is
 * White's captured value minus Black's; a positive value is shown as `+N` on
 * White's row, a negative value as its magnitude on Black's row, and an even
 * count is announced explicitly.
 *
 * @param props - See {@link CapturedPiecesProps}.
 * @returns The captured-pieces panel element.
 */
export function CapturedPieces({ moves = [] }: CapturedPiecesProps) {
  // Tally captures from the move history. White's captures are the Black pieces
  // White removed; Black's captures are the White pieces Black removed.
  const capturedByWhite = emptyCounts();
  const capturedByBlack = emptyCounts();

  for (const move of moves) {
    if (!isPieceType(move.captured)) continue;
    if (move.color === 'w') capturedByWhite[move.captured] += 1;
    else capturedByBlack[move.captured] += 1;
  }

  // Ordered glyph lists (strongest first) plus the summed material values.
  const whiteList: PieceType[] = [];
  const blackList: PieceType[] = [];
  let whiteValue = 0;
  let blackValue = 0;
  for (const type of ORDER) {
    for (let i = 0; i < capturedByWhite[type]; i += 1) whiteList.push(type);
    for (let i = 0; i < capturedByBlack[type]; i += 1) blackList.push(type);
    whiteValue += capturedByWhite[type] * VALUES[type];
    blackValue += capturedByBlack[type] * VALUES[type];
  }

  const diff = whiteValue - blackValue;
  const materialSummary =
    diff > 0
      ? `White leads by ${diff} ${diff === 1 ? 'point' : 'points'}`
      : diff < 0
        ? `Black leads by ${-diff} ${-diff === 1 ? 'point' : 'points'}`
        : 'Material is even';

  return (
    <div role="group" aria-label="Captured pieces and material differential" className="space-y-1">
      {/* White's captures (Black pieces). */}
      <div className="flex flex-wrap items-center gap-0.5 text-xl leading-none text-gray-200">
        <span className="sr-only">
          {describeCaptures('White', capturedByWhite)}
          {diff > 0 ? `; ${materialSummary}` : ''}
        </span>
        {whiteList.length > 0 ? (
          whiteList.map((type, i) => (
            <span key={`w-${type}-${i}`} aria-hidden="true">
              {BLACK_GLYPHS[type]}
            </span>
          ))
        ) : (
          <span aria-hidden="true" className="text-sm text-gray-500">
            —
          </span>
        )}
        {diff > 0 && (
          <span aria-hidden="true" className="ml-1 text-sm text-emerald-400">
            +{diff}
          </span>
        )}
      </div>

      {/* Black's captures (White pieces). */}
      <div className="flex flex-wrap items-center gap-0.5 text-xl leading-none text-gray-200">
        <span className="sr-only">
          {describeCaptures('Black', capturedByBlack)}
          {diff < 0 ? `; ${materialSummary}` : ''}
        </span>
        {blackList.length > 0 ? (
          blackList.map((type, i) => (
            <span key={`b-${type}-${i}`} aria-hidden="true">
              {WHITE_GLYPHS[type]}
            </span>
          ))
        ) : (
          <span aria-hidden="true" className="text-sm text-gray-500">
            —
          </span>
        )}
        {diff < 0 && (
          <span aria-hidden="true" className="ml-1 text-sm text-emerald-400">
            +{-diff}
          </span>
        )}
      </div>

      {/* Neither row carries a badge when material is even; announce it. */}
      {diff === 0 && <span className="sr-only">{materialSummary}</span>}
    </div>
  );
}

export default CapturedPieces;

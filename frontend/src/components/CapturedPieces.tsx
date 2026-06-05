/**
 * CapturedPieces — captured pieces and material differential.
 *
 * A pure, display-only presentational component for the Blitzy Chess SPA. It
 * renders inside `SidePanel.tsx`, which passes the current `fen`. Two rows are
 * shown:
 *
 *   - Row 1: the pieces White has captured (which are BLACK pieces, drawn with
 *     black glyphs), followed by a green `+N` badge when White is ahead.
 *   - Row 2: the pieces Black has captured (which are WHITE pieces, drawn with
 *     white glyphs), followed by a green `+N` badge when Black is ahead.
 *
 * The component derives everything from the FEN on every render; it holds no
 * state, performs no I/O, and imports nothing. It NEVER decides legality — the
 * Python backend (python-chess) is the single source of truth for the rules
 * (project constraint C1). This component only visualizes the position that the
 * authoritative server has already validated.
 *
 * Rendering notes:
 *   - Pieces are real Unicode chess glyphs (no images, no SVG, no canvas).
 *   - Styling uses Tailwind utilities against the dark side-panel theme
 *     (`text-gray-200` glyphs, `text-emerald-400` material badge).
 *
 * @module components/CapturedPieces
 */

/**
 * Props for {@link CapturedPieces}.
 *
 * Only the piece-placement field of the FEN (everything before the first space)
 * is consumed; the side-to-move, castling, en-passant, and clock fields are
 * ignored.
 */
interface CapturedPiecesProps {
  /** Full Forsyth–Edwards Notation string for the current position. */
  fen: string;
}

/**
 * The five non-king piece types, keyed by their lowercase FEN letter. Kings are
 * deliberately excluded: a king is never captured, so it never contributes to
 * captures or to the material differential.
 */
type PieceType = 'p' | 'n' | 'b' | 'r' | 'q';

/**
 * Number of each piece type a single side starts a standard game with. Used to
 * derive "missing" (captured) pieces by differencing against the live counts.
 */
const START_COUNTS: Record<PieceType, number> = { p: 8, n: 2, b: 2, r: 2, q: 1 };

/**
 * Conventional centipawn-free material values (pawn 1 … queen 9). These drive
 * the material differential badge. The king is valueless here by design.
 */
const VALUES: Record<PieceType, number> = { p: 1, n: 3, b: 3, r: 5, q: 9 };

/** White piece glyphs (rendered for the pieces Black has captured). */
const WHITE_GLYPHS: Record<PieceType, string> = { p: '♙', n: '♘', b: '♗', r: '♖', q: '♕' };

/** Black piece glyphs (rendered for the pieces White has captured). */
const BLACK_GLYPHS: Record<PieceType, string> = { p: '♟', n: '♞', b: '♝', r: '♜', q: '♛' };

/**
 * Display order for captured pieces — strongest first (queen → pawn), the
 * customary ordering in chess UIs.
 */
const ORDER: PieceType[] = ['q', 'r', 'b', 'n', 'p'];

/**
 * Build a zeroed per-type tally.
 *
 * @returns A fresh `Record<PieceType, number>` with every count set to 0.
 */
function emptyCounts(): Record<PieceType, number> {
  return { p: 0, n: 0, b: 0, r: 0, q: 0 };
}

/**
 * Count the live pieces on the board for each side from a FEN placement field.
 *
 * The placement field is scanned character by character. Rank separators (`/`)
 * and run-length digits (`1`–`8`, denoting empty squares) are skipped, as are
 * both kings. Uppercase letters are White pieces; lowercase letters are Black
 * pieces.
 *
 * The `(lower in START_COUNTS)` membership test plus the explicit digit guard
 * make the `as PieceType` assertion sound: by the time it runs, `lower` is
 * provably one of the five tracked piece letters.
 *
 * @param placement - The piece-placement field of a FEN (before the first space).
 * @returns Live per-type counts for `white` and `black`.
 */
function countPieces(placement: string): {
  white: Record<PieceType, number>;
  black: Record<PieceType, number>;
} {
  const white = emptyCounts();
  const black = emptyCounts();

  for (const ch of placement) {
    // Skip rank separators and empty-square run-length digits. The inner
    // comparison is wrapped in explicit parentheses so the mixed `||`/`&&`
    // reads unambiguously (no reliance on operator precedence).
    if (ch === '/' || (ch >= '1' && ch <= '8')) continue;

    const lower = ch.toLowerCase();
    // Kings never count toward captures; ignore anything not tracked.
    if (lower === 'k' || !(lower in START_COUNTS)) continue;

    const type = lower as PieceType;
    if (ch === ch.toUpperCase()) white[type] += 1;
    else black[type] += 1;
  }

  return { white, black };
}

/**
 * Render the captured-pieces summary and material differential for a position.
 *
 * Algorithm:
 *   1. Count the live pieces of each side from the FEN placement field.
 *   2. A side's "missing" pieces (start count minus live count) are exactly the
 *      pieces the OPPONENT has captured. White's captures are therefore the
 *      black pieces that are missing, and vice versa.
 *   3. The missing count is clamped with `Math.max(0, …)` so that promotions —
 *      which can push a type ABOVE its starting count (e.g. two queens) — yield
 *      zero captures for that type rather than a negative tally.
 *   4. The material differential is White's captured value minus Black's. A
 *      positive value is shown as `+N` on White's row; a negative value is shown
 *      as `+N` (its magnitude) on Black's row; an even material count shows no
 *      badge.
 *
 * @param props - See {@link CapturedPiecesProps}.
 * @returns The captured-pieces panel element.
 */
export function CapturedPieces({ fen }: CapturedPiecesProps) {
  // Only the placement field is relevant; `?? ''` guards a malformed/empty FEN.
  const placement = fen.split(' ')[0] ?? '';
  const { white, black } = countPieces(placement);

  const capturedByWhite: PieceType[] = []; // black pieces White has taken
  const capturedByBlack: PieceType[] = []; // white pieces Black has taken
  let whiteValue = 0;
  let blackValue = 0;

  for (const type of ORDER) {
    const blackMissing = Math.max(0, START_COUNTS[type] - black[type]);
    const whiteMissing = Math.max(0, START_COUNTS[type] - white[type]);
    for (let i = 0; i < blackMissing; i += 1) capturedByWhite.push(type);
    for (let i = 0; i < whiteMissing; i += 1) capturedByBlack.push(type);
    whiteValue += blackMissing * VALUES[type];
    blackValue += whiteMissing * VALUES[type];
  }

  const diff = whiteValue - blackValue;

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-0.5 text-xl leading-none text-gray-200">
        {capturedByWhite.map((type, i) => (
          <span key={`w-${i}`}>{BLACK_GLYPHS[type]}</span>
        ))}
        {diff > 0 && <span className="ml-1 text-sm text-emerald-400">+{diff}</span>}
      </div>
      <div className="flex items-center gap-0.5 text-xl leading-none text-gray-200">
        {capturedByBlack.map((type, i) => (
          <span key={`b-${i}`}>{WHITE_GLYPHS[type]}</span>
        ))}
        {diff < 0 && <span className="ml-1 text-sm text-emerald-400">+{-diff}</span>}
      </div>
    </div>
  );
}

export default CapturedPieces;

/**
 * PromotionDialog — pawn-promotion piece picker (modal). (AAP §0.5.3)
 *
 * A pure, display-only presentational component for the Blitzy Chess SPA. It is
 * mounted by `../App.tsx` and lets the player choose which piece a promoting
 * pawn becomes (queen, rook, bishop, or knight), rendered in the player's color.
 *
 * Promotion flow (owned by `../App.tsx`, summarized here for context):
 *   1. The player drags or clicks a pawn onto the last rank.
 *   2. `GameBoard.onMove` returns `false` for that move, deferring the default
 *      auto-queen promotion, and App stashes the pending `{ from, to }` squares.
 *   3. App opens this dialog (`open={true}`) with the moving side's `color`.
 *   4. On `onSelect(piece)`, App re-issues the move with the chosen promotion
 *      piece over the WebSocket (the backend, python-chess, remains the sole
 *      authority on legality — constraint C1). On `onCancel`, App discards the
 *      pending move and the pawn stays put.
 *
 * This component holds no state and performs no chess logic: it maps the four
 * promotion pieces to color-correct Unicode glyphs and reports the player's
 * choice (or cancellation) through its callbacks.
 *
 * Rendering / accessibility notes:
 *   - Visibility is gated by the `open` prop, so a parent can mount it
 *     unconditionally. An effect (declared before the early `null` return to keep
 *     the hook order stable per the Rules of Hooks) binds a document-level
 *     Escape-key listener only while open, so keyboard users can dismiss the
 *     pending promotion without a pointer; it is removed on close/unmount.
 *   - The first piece button (Queen) receives initial focus via `autoFocus`, so
 *     a keyboard or screen-reader user lands inside the dialog and can Tab across
 *     the choices and the Cancel control.
 *   - Dismissal is reachable three ways: clicking the full-screen backdrop (a
 *     presentational dismiss target), pressing Escape, and a visible
 *     `<button>Cancel</button>` inside the card. The dialog card stops click
 *     propagation so choosing a piece (or clicking empty card space) never also
 *     triggers `onCancel`.
 *   - The card is a labelled modal dialog (`role="dialog"`, `aria-modal="true"`,
 *     `aria-label`). Each piece is a real `<button type="button">` with a
 *     descriptive `aria-label` (e.g. "Promote to Queen"), so the picker is
 *     keyboard-operable with a visible `:focus-visible` ring; hover and focus
 *     transitions are gated behind `motion-safe`.
 *   - Styled with Tailwind utilities over the project's design tokens
 *     (`bg-panel`, `text-gray-100`); no canvas/SVG/images — the pieces are
 *     Unicode chess glyphs.
 *
 * @module components/PromotionDialog
 */
import { useEffect } from 'react';
import type { Color, PromotionPiece } from '../types';

/**
 * Props for {@link PromotionDialog}.
 */
interface PromotionDialogProps {
  /**
   * Whether the dialog is visible. When `false`, the component renders `null`,
   * so the parent can mount it unconditionally and drive visibility through this
   * prop alone.
   */
  open: boolean;
  /**
   * The promoting side, which selects the glyph set: white glyphs (♕♖♗♘) for
   * `'white'`, black glyphs (♛♜♝♞) for `'black'`.
   */
  color: Color;
  /**
   * Invoked with the chosen promotion piece (`'q' | 'r' | 'b' | 'n'`) when the
   * player picks one. The parent re-issues the deferred move with this piece.
   */
  onSelect: (piece: PromotionPiece) => void;
  /**
   * Invoked when the player dismisses the dialog without choosing a piece —
   * by clicking the backdrop, pressing Escape, or activating the Cancel button.
   * The parent discards the pending move.
   */
  onCancel: () => void;
}

/**
 * The four promotion choices, in conventional value order (queen, rook, bishop,
 * knight). `name` supplies both the per-button `aria-label` text and a stable
 * React `key`. Pawns and kings are never promotion targets, so they are omitted.
 */
const PIECES: { piece: PromotionPiece; name: string }[] = [
  { piece: 'q', name: 'Queen' },
  { piece: 'r', name: 'Rook' },
  { piece: 'b', name: 'Bishop' },
  { piece: 'n', name: 'Knight' },
];

/**
 * Unicode chess glyphs keyed first by side color, then by promotion piece.
 *
 * Typing the map as `Record<Color, Record<PromotionPiece, string>>` makes both
 * levels exhaustive: every `Color` must list a glyph for every `PromotionPiece`,
 * so `GLYPHS[color][piece]` can never resolve to `undefined`, and adding a
 * member to either union in `../types` becomes a compile error until this map is
 * brought back in sync.
 *
 * White uses the outlined glyphs (U+2655–U+2658), black the filled glyphs
 * (U+265B–U+265E). The glyph's outline/fill conveys the side independently of
 * CSS color, so the choice stays unambiguous on the dark button surface.
 */
const GLYPHS: Record<Color, Record<PromotionPiece, string>> = {
  white: { q: '♕', r: '♖', b: '♗', n: '♘' },
  black: { q: '♛', r: '♜', b: '♝', n: '♞' },
};

/**
 * Render the pawn-promotion piece picker.
 *
 * Renders `null` while `open` is `false`. When open, it shows a centered modal
 * card over a dismiss backdrop, with one button per promotion piece drawn in the
 * player's color, plus a Cancel control. The first piece is auto-focused and
 * Escape dismisses the dialog, so the picker is fully keyboard-operable.
 *
 * @param props - See {@link PromotionDialogProps}.
 * @returns The modal element, or `null` when `open` is `false`.
 */
export function PromotionDialog({ open, color, onSelect, onCancel }: PromotionDialogProps) {
  // Escape-key dismissal. The listener is attached to `document` only while the
  // dialog is open and torn down on close/unmount, giving keyboard users a way to
  // cancel the pending promotion without a pointer. This hook is declared BEFORE
  // the early `null` return below so the hook order stays stable across renders
  // (React's Rules of Hooks) — guarding the body on `open` keeps it inert while
  // the dialog is closed.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        onCancel();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open, onCancel]);

  // Visibility is driven entirely by the prop: nothing renders while closed.
  // This short-circuit MUST come after the hook above — no hidden-but-present modal.
  if (!open) return null;

  return (
    <div
      role="presentation"
      onClick={onCancel}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Choose promotion piece"
        onClick={(e) => e.stopPropagation()}
        className="rounded-xl bg-panel p-6 shadow-2xl"
      >
        <h2 className="mb-4 text-center text-lg font-semibold text-gray-100">Promote pawn</h2>
        <div className="flex gap-3">
          {PIECES.map(({ piece, name }, index) => (
            <button
              key={piece}
              type="button"
              // Land keyboard/screen-reader focus inside the dialog on the most
              // common choice (Queen) so the picker is operable without a pointer.
              autoFocus={index === 0}
              onClick={() => onSelect(piece)}
              aria-label={`Promote to ${name}`}
              className="flex h-16 w-16 items-center justify-center rounded-lg bg-gray-700 text-4xl leading-none text-gray-100 hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 motion-safe:transition-colors"
            >
              {GLYPHS[color][piece]}
            </button>
          ))}
        </div>
        {/*
         * Visible, keyboard-reachable cancel control. Backdrop-click and Escape
         * also dismiss, but a focusable button keeps the abort path available to
         * pointer-free users who Tab through the dialog.
         */}
        <button
          type="button"
          onClick={onCancel}
          className="mt-4 w-full rounded-lg bg-gray-700 px-4 py-2 text-sm font-medium text-gray-200 hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 motion-safe:transition-colors"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export default PromotionDialog;

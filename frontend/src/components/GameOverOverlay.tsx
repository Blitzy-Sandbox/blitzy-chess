/**
 * GameOverOverlay — end-of-game result overlay (AAP §0.5.3).
 *
 * A pure, display-only presentational component for the Blitzy Chess SPA. It is
 * routed from `../App.tsx` and rendered on top of the active game once the
 * server reports a terminal position:
 *
 *   <GameOverOverlay gameOver={gameOver} onNewGame={...} onExit={...} />
 *
 * The overlay reports the terminal `result` (checkmate / stalemate / draw /
 * resignation / timeout) and, for a decisive game, the winning side, then offers
 * two actions: "New game" and "Exit to menu".
 *
 * Source of truth — the server's authoritative `game_over` WebSocket message,
 * typed here as {@link GameOverMessage}. The backend (python-chess) is the sole
 * authority on legality and termination (constraint C1); this component never
 * computes a result. It only visualizes the message the backend has already
 * emitted. `gameOver.reason` is a ready-made, human-readable string (e.g.
 * "Black wins by checkmate") and is rendered verbatim — never parsed.
 *
 * Rendering notes:
 *   - Returns `null` when `gameOver` is `null`, so a parent can mount the overlay
 *     unconditionally and let the prop gate visibility (the short-circuit is the
 *     very first statement).
 *   - The result headline is driven by an exhaustive {@link TITLES} map keyed on
 *     the literal `GameResult` union (`Record<GameOverMessage['result'], string>`).
 *     Adding or removing a `GameResult` member is a compile error until the map
 *     is updated, so the headline can never fall through to `undefined`.
 *   - `winner` is `Color | null`; a raw `null` is never interpolated into the
 *     outcome line. Decisive games show `"<Color> wins"` (capitalized); draws and
 *     stalemate show `"Draw"`. The outcome line is suppressed when it would merely
 *     repeat the headline (the pure `draw` case), so the card never shows
 *     "Draw" twice.
 *   - Accessibility: the backdrop is a labelled modal dialog
 *     (`role="dialog" aria-modal="true"`), the headline labels it, and the reason
 *     describes it. Both actions are real `<button type="button">` elements, so
 *     they are keyboard-operable with a visible `:focus-visible` ring; hover and
 *     focus transitions are gated behind `motion-safe`.
 *   - The board and all chrome are styled with Tailwind utilities over the
 *     project's design tokens (`bg-panel`, `text-secondary`); no canvas/SVG/images
 *     are used here.
 *
 * @module components/GameOverOverlay
 */
import type { GameOverMessage } from '../types';

/**
 * Props for {@link GameOverOverlay}.
 */
interface GameOverOverlayProps {
  /**
   * The server's terminal `game_over` message, or `null` while the game is still
   * in progress. When `null`, the component renders nothing, so the parent can
   * mount it unconditionally and drive visibility purely through this prop.
   */
  gameOver: GameOverMessage | null;
  /** Invoked when the player chooses "New game". */
  onNewGame: () => void;
  /** Invoked when the player chooses "Exit to menu". */
  onExit: () => void;
}

/**
 * Result headline keyed on the literal `GameResult` union. Typing the map as
 * `Record<GameOverMessage['result'], string>` makes it exhaustive: if the union
 * in `../types` gains or loses a member, this object stops type-checking until it
 * is brought back in sync, which guarantees every terminal result has a headline.
 */
const TITLES: Record<GameOverMessage['result'], string> = {
  checkmate: 'Checkmate',
  stalemate: 'Stalemate',
  draw: 'Draw',
  resignation: 'Resignation',
  timeout: 'Timeout',
};

/**
 * Capitalize the first character of a string (e.g. `"white"` → `"White"`).
 *
 * Pure and side-effect free; used to render the lowercase wire color
 * (`"white"` / `"black"`) as a title-cased word in the outcome line.
 *
 * @param value - The string to capitalize.
 * @returns The input with its first character upper-cased.
 */
function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * Render the end-of-game result overlay.
 *
 * Renders `null` when no game-over message is present. Otherwise it shows a
 * centered modal card with the result headline, the outcome (winner or draw),
 * the server's human-readable reason, and the "New game" / "Exit to menu"
 * actions.
 *
 * @param props - See {@link GameOverOverlayProps}.
 * @returns The overlay element, or `null` when `gameOver` is `null`.
 */
export function GameOverOverlay({ gameOver, onNewGame, onExit }: GameOverOverlayProps) {
  // Visibility is driven entirely by the prop: nothing renders until the server
  // reports a terminal position. This short-circuit MUST come first.
  if (!gameOver) return null;

  const { result, winner, reason } = gameOver;

  // Decisive games name the victor; draws and stalemate read "Draw". `winner` is
  // `Color | null`, so the guard ensures a raw `null` is never interpolated.
  const outcome = winner ? `${capitalize(winner)} wins` : 'Draw';

  // Suppress the outcome line when it would merely repeat the headline (the pure
  // `draw` case, where both read "Draw"); decisive results and stalemate still
  // show it ("White wins", "Draw" under "Stalemate", etc.).
  const showOutcome = outcome !== TITLES[result];

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="game-over-title"
      aria-describedby={reason ? 'game-over-reason' : undefined}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
    >
      <div className="w-full max-w-sm rounded-xl bg-panel p-8 text-center shadow-2xl">
        <h2 id="game-over-title" className="mb-1 text-3xl font-bold text-gray-100">
          {TITLES[result]}
        </h2>

        {showOutcome && <p className="mb-1 text-lg text-emerald-400">{outcome}</p>}

        {reason && (
          <p id="game-over-reason" className="text-sm text-secondary">
            {reason}
          </p>
        )}

        <div className="mt-6 flex flex-col gap-2">
          <button
            type="button"
            onClick={onNewGame}
            className="rounded-lg bg-emerald-700 px-4 py-3 font-medium text-gray-100 hover:bg-emerald-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400 motion-safe:transition-colors"
          >
            New game
          </button>
          <button
            type="button"
            onClick={onExit}
            className="rounded-lg bg-gray-700 px-4 py-3 font-medium text-gray-100 hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 motion-safe:transition-colors"
          >
            Exit to menu
          </button>
        </div>
      </div>
    </div>
  );
}

export default GameOverOverlay;

/**
 * ModeSelect — the landing / mode-selection screen for the Blitzy Chess SPA.
 *
 * This is the first screen a player sees (AAP §0.5.3). It is a pure,
 * display-only presentational component routed from `../App.tsx` as:
 *
 *   <ModeSelect
 *     onSelectAi={(difficulty) => ...}
 *     onPlayOnline={() => ...}
 *     onWatchSelfPlay={() => ...}
 *   />
 *
 * It offers the three ways into the application:
 *
 *   1. Play vs the AI — one button per difficulty tier (Easy, Medium, Hard).
 *      Each tier's search profile (depth + per-move time budget) is shown as a
 *      blurb and maps, on the backend, to a fixed search depth and time budget:
 *      Easy (depth 4 / 3s), Medium (depth 6 / 8s), Hard (depth 8 / 15s). The
 *      chosen tier is forwarded to the parent, which opens `/ws/game` with it.
 *   2. Play online — enters the multiplayer lobby (create or join a room).
 *   3. Watch self-play — opens the AI-vs-AI demonstration screen.
 *
 * Boundaries: this component holds no state, opens no WebSocket, and issues no
 * HTTP request (transport constraint C16); it only invokes the three callbacks
 * the parent provides. The backend (python-chess) stays the sole authority on
 * the rules and game state (constraint C1).
 *
 * Styling / accessibility: Tailwind utilities over the project's design tokens
 * (`bg-panel` dark surface, `text-secondary` muted text). Every action is a
 * real `<button type="button">` so it is keyboard-operable and never triggers
 * an implicit form submit; hover is gated behind `motion-safe` and focus uses
 * `:focus-visible`. The tier label/blurb pairing means color is never the sole
 * carrier of meaning. No canvas / SVG / images; the board renders only via
 * react-chessboard on the in-game screens, never here.
 *
 * @module components/ModeSelect
 */
import type { Difficulty } from '../types';

/**
 * Props for {@link ModeSelect}. The parent (`App.tsx`) owns all navigation, so
 * the screen is fully controlled through these three callbacks and holds no
 * state of its own.
 */
interface ModeSelectProps {
  /**
   * Invoked with the chosen {@link Difficulty} when the player picks an AI tier.
   * The parent opens the `/ws/game` channel for that tier.
   */
  onSelectAi: (difficulty: Difficulty) => void;
  /** Invoked when the player chooses real-time multiplayer (the online lobby). */
  onPlayOnline: () => void;
  /** Invoked when the player chooses to watch the AI-vs-AI self-play demo. */
  onWatchSelfPlay: () => void;
}

/**
 * The selectable AI difficulty tiers, in ascending strength.
 *
 * Driven from a typed array (rather than three hard-coded buttons) so the tiers
 * share a single render path and `onSelectAi(tier.id)` type-checks against
 * {@link Difficulty} under strict TypeScript — the explicit element-type
 * annotation narrows each `id` literal to `Difficulty`. The `blurb` strings are
 * the exact search profiles from the AAP difficulty table and are display-only;
 * the authoritative depth and time budgets live on the backend.
 */
const TIERS: { id: Difficulty; label: string; blurb: string }[] = [
  { id: 'easy', label: 'Easy', blurb: 'Depth 4 · 3s per move' },
  { id: 'medium', label: 'Medium', blurb: 'Depth 6 · 8s per move' },
  { id: 'hard', label: 'Hard', blurb: 'Depth 8 · 15s per move' },
];

/**
 * Render the landing / mode-selection card.
 *
 * @param props - See {@link ModeSelectProps}.
 * @returns The centered mode-selection screen element.
 */
export function ModeSelect({ onSelectAi, onPlayOnline, onWatchSelfPlay }: ModeSelectProps) {
  return (
    <div className="flex min-h-screen w-full flex-col items-center justify-center p-4">
      <div className="w-full max-w-md rounded-xl bg-panel p-8 shadow-lg">
        <h1 className="mb-1 text-center text-3xl font-bold tracking-tight text-gray-100">
          Blitzy Chess
        </h1>
        <p className="mb-6 text-center text-sm text-secondary">
          Play the engine, challenge a friend, or watch the AI play itself.
        </p>

        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-secondary">
            Play vs AI
          </h2>
          <div className="grid grid-cols-1 gap-2">
            {TIERS.map((tier) => (
              <button
                key={tier.id}
                type="button"
                onClick={() => onSelectAi(tier.id)}
                className="flex items-center justify-between rounded-lg bg-gray-700 px-4 py-3 text-left text-gray-100 hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 motion-safe:transition-colors"
              >
                <span className="font-medium">{tier.label}</span>
                {/* BLITZY [A11Y]: supplementary tier blurb is intentionally muted
                    (text-secondary #9ca3af) per the design source; on the gray-700
                    surface this computes ~3.8:1, below WCAG AA 4.5:1 for small text.
                    Implemented per design and flagged for designer review — the
                    high-contrast tier label carries the meaning, so color is not
                    the sole indicator. */}
                <span className="text-xs text-secondary">{tier.blurb}</span>
              </button>
            ))}
          </div>
        </section>

        <div className="grid grid-cols-1 gap-2">
          <button
            type="button"
            onClick={onPlayOnline}
            className="rounded-lg bg-emerald-700 px-4 py-3 font-medium text-gray-100 hover:bg-emerald-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400 motion-safe:transition-colors"
          >
            Play Online
          </button>
          <button
            type="button"
            onClick={onWatchSelfPlay}
            className="rounded-lg bg-indigo-700 px-4 py-3 font-medium text-gray-100 hover:bg-indigo-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 motion-safe:transition-colors"
          >
            Watch Self-Play
          </button>
        </div>
      </div>
    </div>
  );
}

export default ModeSelect;

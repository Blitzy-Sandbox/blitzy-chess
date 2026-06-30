/**
 * OnlineLobby — multiplayer lobby: create or join a room. (AAP §0.5.3)
 *
 * A pure, display-only presentational component for the Blitzy Chess SPA. It is
 * routed from `../App.tsx` as the "Play Online" screen and offers the two ways a
 * player enters a real-time multiplayer game:
 *
 *   <OnlineLobby
 *     onCreateRoom={...}
 *     onJoinRoom={...}
 *     room={...}
 *     error={...}
 *     waiting={...}
 *   />
 *
 *   1. Create a room — the server seats the creator as White and returns a
 *      shareable 6-character code; the lobby shows that code prominently, names
 *      the assigned color, and (while `waiting`) shows a "Waiting for opponent…"
 *      indicator until the second player joins.
 *   2. Join a room — the player types a 6-character code; the lobby trims,
 *      upper-cases, and format-validates it before forwarding it to the parent.
 *
 * Transport boundary (constraint C16): this component never opens a WebSocket
 * and never issues an HTTP request. The real-time channel lives in
 * `../hooks/useMultiplayerWebSocket.ts`; the lobby only invokes the
 * `onCreateRoom` / `onJoinRoom` callbacks and renders the `room`, `error`, and
 * `waiting` props the parent derives from that hook. The backend (python-chess)
 * stays the sole authority on room state and move legality (constraint C1).
 *
 * Rendering / accessibility notes:
 *   - Before a room exists (`room` is `null`) the lobby shows the "Create Room"
 *     action plus the join form; once `room` is set — after creating OR joining
 *     — those are replaced by the room-code panel, so the screen never offers a
 *     second create/join while the player already holds a seat.
 *   - The join form is a real `<form>`: submitting it (Enter or the button) runs
 *     `handleJoin`, which calls `e.preventDefault()` so the page never reloads,
 *     normalizes the code with `trim().toUpperCase()`, and forwards it only when
 *     it matches the room-code format ({@link ROOM_CODE_RE} — six characters over
 *     the backend alphabet). The Join button is `disabled` until the code is
 *     valid, so a malformed code can never be submitted.
 *   - The code `<input>` has no visible label, so it carries an `aria-label`. It
 *     forces uppercase both on change and on submit, and turns off browser
 *     autofill, autocorrect, and spellcheck so a room code is never mangled. Once
 *     the typed value is non-empty but not yet valid it is flagged with
 *     `aria-invalid` and tied via `aria-describedby` to a `role="alert"` hint
 *     ("Enter a 6-character room code.") rendered just below the field.
 *   - Errors render in a `role="alert"` region; {@link errorText} maps the
 *     common `room_not_found` / `room_full` codes to friendlier copy and falls
 *     back to the server's `error.message` for any other code.
 *   - The room-code panel is an `aria-live="polite"` region so assistive
 *     technology announces the code (and the waiting state) when it appears.
 *   - Styled with Tailwind utilities over the project's design tokens
 *     (`bg-panel` dark surface, `text-secondary` muted text). Focus rings use
 *     `:focus-visible`; hover and the waiting pulse are gated behind
 *     `motion-safe`. No canvas / SVG / images.
 *
 * @module components/OnlineLobby
 */
import { useState, type FormEvent } from 'react';
import type { Color, ErrorMessage } from '../types';

/**
 * Client-side room-code format gate, mirroring the backend's generator alphabet
 * in `backend/chess_ai/rooms/manager.py` (`_CODE_ALPHABET` = uppercase letters
 * and digits with the ambiguous `O 0 I 1` removed, `ROOM_CODE_LENGTH = 6`). A
 * server code can therefore only contain `A–H J–N P–Z 2–9`, exactly six chars.
 *
 * This is a fail-fast UX guard, not an authority check: the server (constraint
 * C1) remains the sole judge of whether a room exists. Keeping the pattern in
 * step with the backend alphabet means a typo with a disallowed character (e.g.
 * the digit `0` or letter `O`) is caught before a doomed join is dispatched.
 */
const ROOM_CODE_RE = /^[A-HJ-NP-Z2-9]{6}$/;

/**
 * Props for {@link OnlineLobby}.
 */
interface OnlineLobbyProps {
  /**
   * Invoked when the player clicks "Create Room". The parent asks the
   * multiplayer hook to open a new room on the server.
   */
  onCreateRoom: () => void;
  /**
   * Invoked with a normalized (trimmed, upper-cased) room code when the player
   * submits the join form. The parent forwards it to the multiplayer hook.
   */
  onJoinRoom: (code: string) => void;
  /**
   * The room the player currently holds, or `null` before one exists. Once set
   * — whether by creating (seated White) or joining (seated Black) — the lobby
   * shows the shareable `code` and the assigned `color` instead of the
   * create/join controls.
   */
  room?: { code: string; color: Color } | null;
  /**
   * The most recent server error to surface (e.g. `room_not_found`,
   * `room_full`), or `null` when there is nothing to report. Rendered in the
   * alert region via {@link errorText}.
   */
  error?: ErrorMessage | null;
  /**
   * `true` once a room has been created but the opponent has not yet joined,
   * which drives the "Waiting for opponent…" indicator. Has no effect until
   * `room` is set.
   */
  waiting?: boolean;
}

/**
 * Map a server {@link ErrorMessage} to user-facing copy.
 *
 * The two codes a player meets most often in the lobby — `room_not_found` and
 * `room_full` — get friendlier, actionable wording. Every other code falls back
 * to the server-supplied `error.message`, so a future or less-common backend
 * code is still shown rather than swallowed.
 *
 * @param error - The server error to describe.
 * @returns The message to render in the alert region.
 */
function errorText(error: ErrorMessage): string {
  switch (error.code) {
    case 'room_not_found':
      return 'Room not found. Check the code and try again.';
    case 'room_full':
      return 'That room is already full.';
    default:
      return error.message;
  }
}

/**
 * Render the multiplayer lobby.
 *
 * Shows the "Create Room" action and a join-by-code form until the player holds
 * a room; thereafter it shows the shareable 6-character code, the assigned
 * color, and (while `waiting`) a "Waiting for opponent…" indicator. Any `error`
 * is surfaced in an alert region above the controls.
 *
 * @param props - See {@link OnlineLobbyProps}.
 * @returns The lobby screen element.
 */
export function OnlineLobby({
  onCreateRoom,
  onJoinRoom,
  room = null,
  error = null,
  waiting = false,
}: OnlineLobbyProps) {
  // The join-code input is the component's only local state; everything else
  // (room, error, waiting) is owned by the parent and the multiplayer hook.
  const [code, setCode] = useState('');

  // Derive submit-eligibility once per render. Normalize exactly as the parent
  // will see it (trim + upper-case), then require the full 6-character backend
  // alphabet via ROOM_CODE_RE so the Join button only enables for a code the
  // server could actually have issued.
  const normalizedCode = code.trim().toUpperCase();
  const canJoin = ROOM_CODE_RE.test(normalizedCode);
  // Show the format hint only once the player has typed something that does not
  // (yet) satisfy the pattern, so an untouched field is not pre-flagged.
  const showCodeHint = normalizedCode.length > 0 && !canJoin;

  /**
   * Handle the join form submission: never reload the page, normalize the code,
   * and forward it to the parent only when it matches the room-code format.
   */
  const handleJoin = (e: FormEvent) => {
    e.preventDefault();
    if (!ROOM_CODE_RE.test(normalizedCode)) return;
    onJoinRoom(normalizedCode);
  };

  return (
    <div className="flex min-h-screen w-full flex-col items-center justify-center p-4">
      <div className="w-full max-w-md rounded-xl bg-panel p-8 shadow-lg">
        <h1 className="mb-6 text-center text-2xl font-bold text-gray-100">Play Online</h1>

        {error && (
          <div
            role="alert"
            className="mb-4 rounded-lg border border-red-700 bg-red-900/40 px-4 py-2 text-sm text-red-200"
          >
            {errorText(error)}
          </div>
        )}

        {room ? (
          <div className="text-center" aria-live="polite">
            <p className="mb-1 text-sm text-secondary">Share this room code:</p>
            <p className="mb-2 font-mono text-4xl font-bold tracking-[0.3em] text-gray-100">
              {room.code}
            </p>
            <p className="text-sm text-secondary">
              You play {room.color === 'white' ? 'White' : 'Black'}
            </p>
            {waiting && (
              <p className="mt-3 text-sm text-emerald-400 motion-safe:animate-pulse">
                Waiting for opponent…
              </p>
            )}
          </div>
        ) : (
          <>
            <button
              type="button"
              onClick={onCreateRoom}
              className="mb-6 w-full rounded-lg bg-emerald-700 px-4 py-3 font-medium text-gray-100 hover:bg-emerald-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400 motion-safe:transition-colors"
            >
              Create Room
            </button>

            <div className="my-4 flex items-center gap-3 text-xs uppercase text-secondary">
              <span className="h-px flex-1 bg-gray-700" />
              or join
              <span className="h-px flex-1 bg-gray-700" />
            </div>

            <form onSubmit={handleJoin} className="flex flex-col gap-2">
              <div className="flex gap-2">
                <input
                  type="text"
                  name="room-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value.toUpperCase())}
                  maxLength={6}
                  placeholder="CODE"
                  aria-label="Room code"
                  aria-invalid={showCodeHint}
                  aria-describedby={showCodeHint ? 'room-code-hint' : undefined}
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  className="flex-1 rounded-lg bg-gray-800 px-4 py-3 text-center font-mono uppercase tracking-widest text-gray-100 outline-none focus-visible:ring-2 focus-visible:ring-emerald-600"
                />
                <button
                  type="submit"
                  disabled={!canJoin}
                  className="rounded-lg bg-gray-700 px-5 py-3 font-medium text-gray-100 hover:bg-gray-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 disabled:cursor-not-allowed disabled:opacity-50 motion-safe:transition-colors"
                >
                  Join
                </button>
              </div>
              {showCodeHint && (
                <p id="room-code-hint" role="alert" className="text-xs text-amber-400">
                  Enter a 6-character room code.
                </p>
              )}
            </form>
          </>
        )}
      </div>
    </div>
  );
}

export default OnlineLobby;

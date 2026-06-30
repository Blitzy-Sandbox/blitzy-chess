import { render, screen, act, fireEvent } from '@testing-library/react';
import App from '../App';
import type { RoomCreatedMessage, StateMessage } from '../types';

// Minimal WebSocket double shared by both flows: the App composes the real
// WebSocket hooks, so driving gameplay end-to-end means feeding authoritative
// `state` frames through this stub exactly as the server would.
class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];

  url: string;
  readyState: number = MockWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  triggerOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  triggerMessage(payload: unknown): void {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }));
  }
}

function lastInstance(): MockWebSocket {
  const inst = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  if (!inst) {
    throw new Error('No MockWebSocket instance was created');
  }
  return inst;
}

// Authoritative position after 1. e4 d5 2. exd5: White has captured one (Black)
// pawn, so the captured-pieces panel must report a +1 material differential.
const STATE_AFTER_CAPTURE: StateMessage = {
  type: 'state',
  fen: 'rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2',
  move_history: ['e4', 'd5', 'exd5'],
  turn: 'black',
  status: 'active',
  in_check: false,
  last_move: { from_square: 'e4', to_square: 'd5' },
  winner: null,
  result: null,
};

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
  fetchSpy = vi.fn();
  vi.stubGlobal('fetch', fetchSpy);
  localStorage.clear();
  // Boot every test from the app root (mode-select) by default. The self-play
  // routing test below sets `/self-play` itself; resetting here keeps a prior
  // test from leaving the path on `/self-play` and booting the wrong screen.
  window.history.pushState({}, '', '/');
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('App captured-pieces wiring (AAP §0.5.3)', () => {
  it('renders captured pieces and the material differential during an AI game', () => {
    render(<App />);

    // Mode select → start an AI game on the Easy tier.
    fireEvent.click(screen.getByRole('button', { name: /easy/i }));

    // The AI screen opens the /ws/game socket; feed it the authoritative state.
    const ws = lastInstance();
    expect(ws.url).toContain('/ws/game');
    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage(STATE_AFTER_CAPTURE));

    // The side panel's captured-pieces summary must reflect the server history,
    // not an empty default — proving App forwards `capturedMoves` to SidePanel.
    expect(
      screen.getByText(/White has captured 1 pawn; White leads by 1 point/),
    ).toBeInTheDocument();
  });

  it('renders captured pieces and the material differential during a multiplayer game', () => {
    render(<App />);

    // Mode select → online lobby.
    fireEvent.click(screen.getByRole('button', { name: /play online/i }));

    // The online screen opens the /ws/multiplayer socket on mount.
    const ws = lastInstance();
    expect(ws.url).toContain('/ws/multiplayer');
    act(() => ws.triggerOpen());

    // Create the room, then receive the room assignment (still in the lobby).
    fireEvent.click(screen.getByRole('button', { name: /create room/i }));
    const created: RoomCreatedMessage = {
      type: 'room_created',
      code: 'ABC123',
      color: 'white',
      player_token: 'tok-1',
    };
    act(() => ws.triggerMessage(created));

    // The activation state moves into the game phase and drives the side panel.
    act(() => ws.triggerMessage(STATE_AFTER_CAPTURE));

    expect(
      screen.getByText(/White has captured 1 pawn; White leads by 1 point/),
    ).toBeInTheDocument();
  });
});

describe('App self-play URL routing (Issue 2 — recorded-demo entry point)', () => {
  it('boots directly into SelfPlayView when loaded at /self-play', () => {
    // The self-play recorder navigates Chromium straight to `/self-play`; the SPA
    // must mount SelfPlayView (which installs window.__BLITZY_SELF_PLAY__ for the
    // runner) rather than the default mode-select landing screen.
    window.history.pushState({}, '', '/self-play');
    render(<App />);

    // SelfPlayView's heading + commentary feed are present...
    expect(screen.getByRole('heading', { name: /self-play demonstration/i })).toBeInTheDocument();
    expect(screen.getByText(/waiting for the self-play feed/i)).toBeInTheDocument();
    // ...and the mode-select landing heading is NOT rendered.
    expect(screen.queryByRole('heading', { name: /^blitzy chess$/i })).not.toBeInTheDocument();

    // The render hook the runner waits on is installed by the mounted view, so
    // the runner's `wait_for_function(ready)` now resolves and `render(state)`
    // advances the live board on the recording.
    expect(window.__BLITZY_SELF_PLAY__?.ready).toBe(true);
  });
});

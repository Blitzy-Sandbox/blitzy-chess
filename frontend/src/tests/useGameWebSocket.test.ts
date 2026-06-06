import { renderHook, act } from '@testing-library/react';
import { useGameWebSocket } from '../hooks/useGameWebSocket';
import type { AiThinkingMessage, ErrorMessage, GameOverMessage, StateMessage } from '../types';

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

  triggerClose(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  lastSent(): unknown {
    return JSON.parse(this.sent[this.sent.length - 1]);
  }
}

function lastInstance(): MockWebSocket {
  const inst = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  if (!inst) {
    throw new Error('No MockWebSocket instance was created');
  }
  return inst;
}

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
  fetchSpy = vi.fn();
  vi.stubGlobal('fetch', fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function mountOpen(opts: Parameters<typeof useGameWebSocket>[0]) {
  const rendered = renderHook(() => useGameWebSocket(opts));
  const ws = lastInstance();
  act(() => ws.triggerOpen());
  return { ...rendered, ws };
}

describe('useGameWebSocket', () => {
  it('connects to a relative /ws/game URL carrying the difficulty and color', () => {
    renderHook(() => useGameWebSocket({ difficulty: 'medium', humanColor: 'white' }));
    const ws = lastInstance();
    expect(ws.url).toContain('/ws/game');
    expect(ws.url).toContain('difficulty=medium');
    expect(ws.url).toContain('color=white');
    expect(ws.url).not.toContain('8000');
    expect(ws.url).not.toMatch(/^http/);
  });

  it('marks connected once the socket opens', () => {
    const { result } = renderHook(() => useGameWebSocket({ difficulty: 'easy' }));
    expect(result.current.connected).toBe(false);
    act(() => lastInstance().triggerOpen());
    expect(result.current.connected).toBe(true);
  });

  it('sendMove serializes a snake_case move message with promotion defaulting to null', () => {
    const { result, ws } = mountOpen({ difficulty: 'hard' });
    act(() => result.current.sendMove('e2', 'e4'));
    expect(ws.lastSent()).toEqual({
      type: 'move',
      from_square: 'e2',
      to_square: 'e4',
      promotion: null,
    });
  });

  it('sendMove forwards the promotion piece when supplied', () => {
    const { result, ws } = mountOpen({ difficulty: 'hard' });
    act(() => result.current.sendMove('e7', 'e8', 'q'));
    expect(ws.lastSent()).toEqual({
      type: 'move',
      from_square: 'e7',
      to_square: 'e8',
      promotion: 'q',
    });
  });

  it('resign sends a resign message', () => {
    const { result, ws } = mountOpen({ difficulty: 'easy' });
    act(() => result.current.resign());
    expect(ws.lastSent()).toEqual({ type: 'resign' });
  });

  it('maps inbound state / ai_thinking / game_over / error onto the returned values', () => {
    const { result, ws } = mountOpen({ difficulty: 'medium' });

    const stateMsg: StateMessage = {
      type: 'state',
      fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',
      move_history: ['e4'],
      turn: 'black',
      status: 'active',
      in_check: false,
      last_move: { from_square: 'e2', to_square: 'e4' },
      winner: null,
      result: null,
    };
    act(() => ws.triggerMessage(stateMsg));
    expect(result.current.state?.fen).toBe(stateMsg.fen);
    expect(result.current.state?.turn).toBe('black');

    const thinking: AiThinkingMessage = {
      type: 'ai_thinking',
      depth: 6,
      evaluation: 35,
      pv: ['e4', 'e5'],
      nodes: 12000,
      time_s: 0.5,
      nps: 24000,
      mate_in: null,
      seldepth: 8,
    };
    act(() => ws.triggerMessage(thinking));
    expect(result.current.aiThinking?.depth).toBe(6);
    expect(result.current.aiThinking?.pv).toEqual(['e4', 'e5']);

    const over: GameOverMessage = {
      type: 'game_over',
      result: 'checkmate',
      winner: 'white',
      reason: 'Checkmate',
    };
    act(() => ws.triggerMessage(over));
    expect(result.current.gameOver?.result).toBe('checkmate');
    expect(result.current.gameOver?.winner).toBe('white');

    const err: ErrorMessage = {
      type: 'error',
      code: 'illegal_move',
      message: 'Illegal move',
    };
    act(() => ws.triggerMessage(err));
    expect(result.current.error?.code).toBe('illegal_move');
  });

  it('newGame clears state and opens a fresh socket', () => {
    const { result } = renderHook(() => useGameWebSocket({ difficulty: 'medium' }));
    const first = lastInstance();
    act(() => first.triggerOpen());

    const stateMsg: StateMessage = {
      type: 'state',
      fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      move_history: [],
      turn: 'white',
      status: 'active',
      in_check: false,
      last_move: null,
      winner: null,
      result: null,
    };
    act(() => first.triggerMessage(stateMsg));
    expect(result.current.state).not.toBeNull();

    act(() => result.current.newGame());
    expect(MockWebSocket.instances.length).toBe(2);
    expect(first.readyState).toBe(MockWebSocket.CLOSED);
    expect(result.current.state).toBeNull();
  });

  it('reconnects after an unexpected close to the same /ws/game URL', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useGameWebSocket({ difficulty: 'medium', humanColor: 'white' }),
    );
    const first = lastInstance();
    act(() => first.triggerOpen());
    expect(result.current.connected).toBe(true);

    // Simulate a server-side drop: the hook marks the connection down and
    // schedules a backoff reconnect timer.
    act(() => first.triggerClose());
    expect(result.current.connected).toBe(false);

    // Advancing the pending timer fires the reconnect, which opens a fresh
    // socket to the SAME /ws/game endpoint carrying difficulty and color.
    act(() => {
      vi.runOnlyPendingTimers();
    });
    const second = lastInstance();
    expect(second).not.toBe(first);
    expect(second.url).toContain('/ws/game');
    expect(second.url).toContain('difficulty=medium');
    expect(second.url).toContain('color=white');

    // The hook stays disconnected until the new socket actually opens.
    expect(result.current.connected).toBe(false);
    act(() => second.triggerOpen());
    expect(result.current.connected).toBe(true);
  });

  it('never uses fetch/REST for game moves (C16)', () => {
    const { result } = mountOpen({ difficulty: 'easy' });
    act(() => result.current.sendMove('e2', 'e4'));
    act(() => result.current.resign());
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('closes the socket on unmount', () => {
    const { unmount } = renderHook(() => useGameWebSocket({ difficulty: 'easy' }));
    const ws = lastInstance();
    act(() => ws.triggerOpen());
    unmount();
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);
  });
});

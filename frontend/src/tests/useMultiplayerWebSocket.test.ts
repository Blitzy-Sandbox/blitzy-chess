import { renderHook, act } from '@testing-library/react';
import { useMultiplayerWebSocket } from '../hooks/useMultiplayerWebSocket';
import type {
  ErrorMessage,
  GameOverMessage,
  RoomCreatedMessage,
  RoomJoinedMessage,
  StateMessage,
} from '../types';

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
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  localStorage.clear();
});

function mountOpen() {
  const rendered = renderHook(() => useMultiplayerWebSocket());
  const ws = lastInstance();
  act(() => ws.triggerOpen());
  return { ...rendered, ws };
}

describe('useMultiplayerWebSocket', () => {
  it('connects to the relative /ws/multiplayer endpoint', () => {
    renderHook(() => useMultiplayerWebSocket());
    const ws = lastInstance();
    expect(ws.url).toContain('/ws/multiplayer');
    expect(ws.url).not.toContain('8000');
    expect(ws.url).not.toMatch(/^http/);
  });

  it('createRoom sends a create_room message', () => {
    const { result, ws } = mountOpen();
    act(() => result.current.createRoom());
    expect(ws.lastSent()).toEqual({ type: 'create_room' });
  });

  it('joinRoom sends a join_room message with the code', () => {
    const { result, ws } = mountOpen();
    act(() => result.current.joinRoom('ABC123'));
    expect(ws.lastSent()).toEqual({ type: 'join_room', code: 'ABC123' });
  });

  it('room_created populates room as white and persists the player token', () => {
    const { result, ws } = mountOpen();
    const msg: RoomCreatedMessage = {
      type: 'room_created',
      code: 'ABC123',
      color: 'white',
      player_token: 'tok-1',
    };
    act(() => ws.triggerMessage(msg));
    expect(result.current.room).toEqual({ code: 'ABC123', color: 'white', playerToken: 'tok-1' });
    expect(localStorage.getItem('blitzy-chess:mp-token:ABC123')).toBe('tok-1');
  });

  it('room_joined populates room as black and persists the player token', () => {
    const { result, ws } = mountOpen();
    const msg: RoomJoinedMessage = {
      type: 'room_joined',
      code: 'XYZ789',
      color: 'black',
      player_token: 'tok-2',
    };
    act(() => ws.triggerMessage(msg));
    expect(result.current.room).toEqual({ code: 'XYZ789', color: 'black', playerToken: 'tok-2' });
    expect(localStorage.getItem('blitzy-chess:mp-token:XYZ789')).toBe('tok-2');
  });

  it('sendMove serializes a snake_case move message', () => {
    const { result, ws } = mountOpen();
    act(() => result.current.sendMove('e2', 'e4'));
    expect(ws.lastSent()).toEqual({
      type: 'move',
      from_square: 'e2',
      to_square: 'e4',
      promotion: null,
    });
  });

  it('resign includes the player_token once a room is established', () => {
    const { result, ws } = mountOpen();
    const created: RoomCreatedMessage = {
      type: 'room_created',
      code: 'ABC123',
      color: 'white',
      player_token: 'tok-1',
    };
    act(() => ws.triggerMessage(created));
    act(() => result.current.resign());
    expect(ws.lastSent()).toEqual({ type: 'resign', player_token: 'tok-1' });
  });

  it('maps inbound state / game_over / error onto the returned values', () => {
    const { result, ws } = mountOpen();

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
    act(() => ws.triggerMessage(stateMsg));
    expect(result.current.state?.turn).toBe('white');

    const over: GameOverMessage = {
      type: 'game_over',
      result: 'resignation',
      winner: 'black',
      reason: 'White resigned',
    };
    act(() => ws.triggerMessage(over));
    expect(result.current.gameOver?.result).toBe('resignation');

    const err: ErrorMessage = { type: 'error', code: 'room_not_found', message: 'No such room' };
    act(() => ws.triggerMessage(err));
    expect(result.current.error?.code).toBe('room_not_found');
  });

  it('reconnects after an unexpected close and re-announces with the stored token', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useMultiplayerWebSocket());
    const first = lastInstance();
    act(() => first.triggerOpen());
    act(() => result.current.createRoom());

    const created: RoomCreatedMessage = {
      type: 'room_created',
      code: 'ABC123',
      color: 'white',
      player_token: 'tok-1',
    };
    act(() => first.triggerMessage(created));

    // Simulate a server-side drop → hook schedules a reconnect timer.
    act(() => first.triggerClose());
    act(() => {
      vi.runOnlyPendingTimers();
    });

    const second = lastInstance();
    expect(second).not.toBe(first);
    act(() => second.triggerOpen());
    expect(second.lastSent()).toEqual({
      type: 'reconnect',
      code: 'ABC123',
      player_token: 'tok-1',
    });
  });

  it('never uses fetch/REST for moves (C16)', () => {
    const { result } = mountOpen();
    act(() => result.current.createRoom());
    act(() => result.current.sendMove('e2', 'e4'));
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

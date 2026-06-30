import { renderHook, act } from '@testing-library/react';
import { useGameState } from '../hooks/useGameState';

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

describe('useGameState (local chess.js display mirror)', () => {
  it('initializes to the standard starting position', () => {
    const { result } = renderHook(() => useGameState());
    expect(result.current.fen).toBe(START_FEN);
    expect(result.current.turn).toBe('white');
    expect(result.current.inCheck).toBe(false);
  });

  it('syncFromFen updates fen and turn from a server position', () => {
    const { result } = renderHook(() => useGameState());
    act(() => {
      result.current.syncFromFen('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1');
    });
    expect(result.current.turn).toBe('black');
    expect(result.current.fen.split(' ')[0]).toBe('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR');
  });

  it('legalTargets returns the legal destinations for a movable piece', () => {
    const { result } = renderHook(() => useGameState());
    expect([...result.current.legalTargets('e2')].sort()).toEqual(['e3', 'e4']);
  });

  it('legalTargets returns an empty list for an empty square', () => {
    const { result } = renderHook(() => useGameState());
    expect(result.current.legalTargets('e4')).toEqual([]);
  });

  it('reports inCheck when the side to move is in check', () => {
    const { result } = renderHook(() => useGameState());
    act(() => {
      result.current.syncFromFen('4k3/8/8/8/7b/8/8/4K3 w - - 0 1');
    });
    expect(result.current.inCheck).toBe(true);
  });

  it('isPromotion is true only for a pawn reaching the last rank', () => {
    const { result } = renderHook(() => useGameState());
    act(() => {
      result.current.syncFromFen('7k/4P3/8/8/8/8/8/4K3 w - - 0 1');
    });
    expect(result.current.isPromotion('e7', 'e8')).toBe(true);
    expect(result.current.isPromotion('e1', 'e2')).toBe(false);
  });

  it('kingSquare locates each side king', () => {
    const { result } = renderHook(() => useGameState());
    expect(result.current.kingSquare('white')).toBe('e1');
    expect(result.current.kingSquare('black')).toBe('e8');
  });

  it('is a display-only mirror: an invalid FEN is ignored without throwing (C1)', () => {
    const { result } = renderHook(() => useGameState());
    const before = result.current.fen;
    act(() => {
      result.current.syncFromFen('not-a-valid-fen');
    });
    // chess.js v1 throws on invalid load; the mirror swallows it and keeps the
    // last good position. The server, not this hook, decides legality.
    expect(result.current.fen).toBe(before);
  });
});

import { render } from '@testing-library/react';
import { GameBoard } from '../components/GameBoard';

const hoisted = vi.hoisted(() => ({
  chessboardProps: { current: undefined as Record<string, unknown> | undefined },
}));

vi.mock('react-chessboard', () => {
  const Chessboard = (props: Record<string, unknown>) => {
    hoisted.chessboardProps.current = props;
    return null;
  };
  return { Chessboard, default: Chessboard };
});

function lastProps(): Record<string, unknown> {
  const p = hoisted.chessboardProps.current;
  if (!p) {
    throw new Error('Chessboard was not rendered');
  }
  return p;
}

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

beforeEach(() => {
  hoisted.chessboardProps.current = undefined;
});

describe('GameBoard (renders only via react-chessboard — C15)', () => {
  it('passes the FEN and orientation through to react-chessboard', () => {
    render(<GameBoard fen={START_FEN} orientation="white" onMove={() => true} />);
    const props = lastProps();
    expect(props.position).toBe(START_FEN);
    expect(props.boardOrientation).toBe('white');
  });

  it("uses the 'start' placeholder position for an empty FEN", () => {
    render(<GameBoard fen="" orientation="white" onMove={() => true} />);
    expect(lastProps().position).toBe('start');
  });

  it('applies the Lichess board palette', () => {
    render(<GameBoard fen={START_FEN} orientation="white" onMove={() => true} />);
    const props = lastProps();
    // GameBoard forwards the lichess palette as the shared CSS-variable tokens
    // (--board-light / --board-dark), which resolve to #EED8B5 / #AB7A53 at render
    // (see ../styles/index.css). The wrapper passes the token reference, so the
    // captured value is the token rather than the resolved hex.
    expect(props.customLightSquareStyle).toMatchObject({ backgroundColor: 'var(--board-light)' });
    expect(props.customDarkSquareStyle).toMatchObject({ backgroundColor: 'var(--board-dark)' });
  });

  it('caps the board width at 640px', () => {
    render(<GameBoard fen={START_FEN} orientation="white" onMove={() => true} boardWidth={900} />);
    expect(lastProps().boardWidth).toBe(640);
  });

  it('invokes onMove when a piece is dropped', () => {
    const onMove = vi.fn(() => true);
    render(<GameBoard fen={START_FEN} orientation="white" onMove={onMove} />);
    const onPieceDrop = lastProps().onPieceDrop as (source: string, target: string) => boolean;
    const accepted = onPieceDrop('e2', 'e4');
    expect(onMove).toHaveBeenCalledWith('e2', 'e4');
    expect(accepted).toBe(true);
  });

  it('invokes onSquareClick when a square is clicked', () => {
    const onSquareClick = vi.fn();
    render(
      <GameBoard
        fen={START_FEN}
        orientation="white"
        onMove={() => true}
        onSquareClick={onSquareClick}
      />,
    );
    const handler = lastProps().onSquareClick as (square: string) => void;
    handler('e4');
    expect(onSquareClick).toHaveBeenCalled();
    expect(onSquareClick.mock.calls[0][0]).toBe('e4');
  });

  it('disables dragging when draggable is false', () => {
    render(<GameBoard fen={START_FEN} orientation="white" onMove={() => true} draggable={false} />);
    expect(lastProps().arePiecesDraggable).toBe(false);
  });
});

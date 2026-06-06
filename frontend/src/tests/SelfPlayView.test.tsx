import { render, screen, act, fireEvent } from '@testing-library/react';
import { SelfPlayView } from '../components/SelfPlayView';

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

interface SelfPlayBridge {
  ready: boolean;
  render: (state: Record<string, unknown>) => void;
  reset?: () => void;
}

function readBridge(): SelfPlayBridge | undefined {
  return (window as unknown as { __BLITZY_SELF_PLAY__?: SelfPlayBridge }).__BLITZY_SELF_PLAY__;
}

function getBridge(): SelfPlayBridge {
  const bridge = readBridge();
  if (!bridge) {
    throw new Error('Self-play bridge was not installed on window');
  }
  return bridge;
}

const FEN_AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1';

beforeEach(() => {
  hoisted.chessboardProps.current = undefined;
});

describe('SelfPlayView', () => {
  it('installs the self-play bridge on window while mounted and removes it on unmount', () => {
    const { container, unmount } = render(<SelfPlayView onExit={() => {}} />);
    expect(readBridge()).toBeDefined();
    expect(container.textContent ?? '').toContain('Waiting for the self-play feed');
    unmount();
    expect(readBridge()).toBeUndefined();
  });

  it('appends commentary and updates the board when the bridge receives a move', () => {
    const { container } = render(<SelfPlayView onExit={() => {}} />);
    act(() => {
      getBridge().render({
        fen: FEN_AFTER_E4,
        san: 'e4',
        evalCp: 30,
        moveNumber: 1,
        whiteTier: 'Hard',
        blackTier: 'Medium',
      });
    });
    const text = (container.textContent ?? '').replace(/\s+/g, ' ');
    expect(text).toContain('e4'); // SAN in the commentary panel
    expect(text).toContain('+0.30'); // evalCp 30 → (30/100).toFixed(2) with sign
    expect(hoisted.chessboardProps.current?.position).toBe(FEN_AFTER_E4);
  });

  it('shows the white and black tiers from the feed', () => {
    const { container } = render(<SelfPlayView onExit={() => {}} />);
    act(() => {
      getBridge().render({
        fen: FEN_AFTER_E4,
        san: 'e4',
        evalCp: 30,
        moveNumber: 1,
        whiteTier: 'Hard',
        blackTier: 'Medium',
      });
    });
    const text = container.textContent ?? '';
    expect(text).toContain('Hard');
    expect(text).toContain('Medium');
  });

  it('calls onExit when the exit control is clicked', () => {
    const onExit = vi.fn();
    render(<SelfPlayView onExit={onExit} />);
    fireEvent.click(screen.getByRole('button', { name: /exit to menu/i }));
    expect(onExit).toHaveBeenCalledTimes(1);
  });
});

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SidePanel } from '../components/SidePanel';
import type { AiThinkingMessage } from '../types';

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const WHITE_UP_A_PAWN_FEN = 'rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2';

const noop = () => {};

describe('SidePanel', () => {
  it('shows the AI-thinking block with depth, evaluation and principal variation', () => {
    const aiThinking: AiThinkingMessage = {
      type: 'ai_thinking',
      depth: 7,
      evaluation: 120,
      pv: ['Nf3', 'Nc6'],
      nodes: 24500,
      time_s: null,
      nps: null,
      mate_in: null,
      seldepth: null,
    };
    const { container } = render(
      <SidePanel
        status="Your move"
        turn="white"
        moveHistory={['e4', 'e5']}
        aiThinking={aiThinking}
        fen={START_FEN}
        onResign={noop}
        onFlip={noop}
        onNewGame={noop}
      />,
    );
    expect(screen.getByText(/ai thinking/i)).toBeInTheDocument();
    expect(screen.getByText('+1.20 (+120cp)')).toBeInTheDocument();
    expect(screen.getByText('Nf3 Nc6')).toBeInTheDocument();
    expect(container.textContent ?? '').toContain('7');
    expect(container.textContent ?? '').toContain('Your move');
  });

  it('hides the AI-thinking block when no aiThinking is provided', () => {
    render(
      <SidePanel
        status="Your move"
        turn="white"
        moveHistory={[]}
        fen={START_FEN}
        onResign={noop}
        onFlip={noop}
        onNewGame={noop}
      />,
    );
    expect(screen.queryByText(/ai thinking/i)).toBeNull();
  });

  it('fires the control callbacks when the buttons are clicked', async () => {
    const onResign = vi.fn();
    const onFlip = vi.fn();
    const onNewGame = vi.fn();
    const user = userEvent.setup();
    render(
      <SidePanel
        status="Your move"
        turn="white"
        moveHistory={[]}
        fen={START_FEN}
        onResign={onResign}
        onFlip={onFlip}
        onNewGame={onNewGame}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'New game' }));
    await user.click(screen.getByRole('button', { name: 'Flip board' }));
    await user.click(screen.getByRole('button', { name: 'Resign' }));
    expect(onNewGame).toHaveBeenCalledTimes(1);
    expect(onFlip).toHaveBeenCalledTimes(1);
    expect(onResign).toHaveBeenCalledTimes(1);
  });

  it('renders the move history and captured pieces within the panel', () => {
    const { container } = render(
      <SidePanel
        status="Your move"
        turn="white"
        moveHistory={['e4', 'd5', 'exd5']}
        capturedMoves={[{ color: 'w', captured: 'p' }]}
        fen={WHITE_UP_A_PAWN_FEN}
        onResign={noop}
        onFlip={noop}
        onNewGame={noop}
      />,
    );
    const text = (container.textContent ?? '').replace(/\s+/g, ' ');
    expect(text).toContain('exd5'); // from MoveHistory (paired list)
    expect(text).toContain('+1'); // from CapturedPieces material differential
    expect(text).toContain('white'); // turn indicator
  });
});

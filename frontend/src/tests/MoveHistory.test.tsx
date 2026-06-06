import { render } from '@testing-library/react';
import { MoveHistory } from '../components/MoveHistory';

function normalize(text: string | null): string {
  return (text ?? '').replace(/\s+/g, ' ').trim();
}

function moveNumberCount(text: string): number {
  return (text.match(/\d+\./g) ?? []).length;
}

describe('MoveHistory (paired algebraic notation — C7)', () => {
  it('renders three numbered rows for a five-move list (trailing white-only move)', () => {
    const { container } = render(<MoveHistory moves={['e4', 'e5', 'Nf3', 'Nc6', 'Bb5']} />);
    const text = normalize(container.textContent);
    expect(moveNumberCount(text)).toBe(3);
    expect(text).toContain('1.');
    expect(text).toContain('2.');
    expect(text).toContain('3.');
    for (const san of ['e4', 'e5', 'Nf3', 'Nc6', 'Bb5']) {
      expect(text).toContain(san);
    }
  });

  it('renders two numbered rows for a three-move list with no black reply on the last row', () => {
    const { container } = render(<MoveHistory moves={['e4', 'e5', 'Nf3']} />);
    const text = normalize(container.textContent);
    expect(moveNumberCount(text)).toBe(2);
    expect(text).toContain('1.');
    expect(text).toContain('2.');
    expect(text).not.toContain('3.');
    for (const san of ['e4', 'e5', 'Nf3']) {
      expect(text).toContain(san);
    }
  });

  it('renders the empty-state message when there are no moves', () => {
    const { container } = render(<MoveHistory moves={[]} />);
    const text = normalize(container.textContent);
    expect(text).toContain('No moves yet.');
    expect(moveNumberCount(text)).toBe(0);
  });
});

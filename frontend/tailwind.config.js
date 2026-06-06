/**
 * Tailwind CSS 3 configuration (ES module).
 *
 * Single source of truth for the application's design tokens. The React SPA
 * consumes these as utility classes — for example `bg-board-light`,
 * `bg-board-dark`, `bg-panel`, `bg-panel-inset`, `text-secondary`, and
 * `max-w-board`.
 *
 * Design tokens:
 *   board-light    #EED8B5  light chess square (Lichess-style)
 *   board-dark     #AB7A53  dark chess square (Lichess-style)
 *   panel          #1e1e1e  dark side-panel surface
 *   panel-inset    #181818  inset surface on `panel` (black/20 composited)
 *   text-secondary #9ca3af  muted text meeting WCAG AA 4.5:1 on the panel
 *   board (maxW)   640px    chessboard maximum width cap
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  // Sources Tailwind's JIT engine scans for class usage: the SPA host document
  // and every TypeScript/TSX source file.
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        'board-light': '#EED8B5',
        'board-dark': '#AB7A53',
        panel: '#1e1e1e',
        // Inset surface for nested regions (e.g. the move-history scroll box)
        // sitting on `panel`; equals black/20 composited over #1e1e1e. Consumed
        // as `bg-panel-inset`.
        'panel-inset': '#181818',
      },
      // Secondary/muted TEXT color, kept text-specific so the utility reads
      // cleanly as `text-secondary`. #9ca3af measures ~6.6:1 on the dark panel
      // (WCAG AA pass for normal text), replacing the failing gray-500 (~3.45:1).
      textColor: {
        secondary: '#9ca3af',
      },
      maxWidth: {
        board: '640px',
      },
    },
  },
  plugins: [],
};

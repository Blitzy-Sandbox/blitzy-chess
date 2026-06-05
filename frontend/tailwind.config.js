/**
 * Tailwind CSS 3 configuration (ES module).
 *
 * Single source of truth for the application's design tokens. The React SPA
 * consumes these as utility classes — for example `bg-board-light`,
 * `bg-board-dark`, `bg-panel`, and `max-w-board`.
 *
 * Design tokens:
 *   board-light  #EED8B5  light chess square (Lichess-style)
 *   board-dark   #AB7A53  dark chess square (Lichess-style)
 *   panel        #1e1e1e  dark side-panel surface
 *   board (maxW) 640px    chessboard maximum width cap
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
      },
      maxWidth: {
        board: '640px',
      },
    },
  },
  plugins: [],
};

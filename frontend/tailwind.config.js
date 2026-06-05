/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Board palette (Lichess-style) and dark side panel, per the spec.
        board: {
          light: '#EED8B5',
          dark: '#AB7A53',
        },
        panel: '#1e1e1e',
      },
      maxWidth: {
        board: '640px',
      },
    },
  },
  plugins: [],
};

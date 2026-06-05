/**
 * PostCSS pipeline configuration (ES module).
 *
 * Vite auto-detects this file and runs it over the application's CSS
 * (`src/styles/index.css`) during both `dev` and `build`. This is the Tailwind
 * CSS 3 PostCSS-plugin integration model, so plugins are declared in object form
 * and resolved by name at build time (no explicit `import` is required).
 *
 * Plugins (applied top-to-bottom — Tailwind first, Autoprefixer last):
 *   tailwindcss   Compiles the `@tailwind` directives into utility CSS, driven
 *                 by `tailwind.config.js`.
 *   autoprefixer  Adds vendor prefixes to the generated CSS for browser support.
 *
 * @type {import('postcss-load-config').Config}
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};

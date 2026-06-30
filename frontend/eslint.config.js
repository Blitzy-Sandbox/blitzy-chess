/**
 * ESLint 9 flat configuration (ES module) for the Blitzy Chess frontend.
 *
 * Entry point for `make lint` (npm run lint -> `eslint .`). It lints the
 * TypeScript / React single-page application source under `src/`.
 *
 * Composition (order is significant):
 *   1. Global ignore patterns.
 *   2. typescript-eslint recommended (non type-checked) — the TypeScript parser
 *      plus a pragmatic set of syntactic rules.
 *   3. eslint-plugin-react-hooks (recommended) — enforces the Rules of Hooks and
 *      flags missing effect dependencies.
 *   4. eslint-plugin-react-refresh (Vite preset) — keeps modules fast-refresh
 *      friendly for the Vite dev server.
 *   5. Project block — browser/DOM globals and two TypeScript rules relaxed to
 *      warnings.
 *
 * The rationale for selecting this toolchain (and for declaring globals inline
 * rather than depending on the `globals` package) is recorded in
 * docs/decision-log.md, not in these comments, per the Explainability rule.
 */
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

export default tseslint.config(
  // 1) Build output, coverage reports, and installed dependencies are never linted.
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**'],
  },

  // 2) typescript-eslint's recommended (non type-checked) preset. Supplies the
  //    TypeScript parser plus syntactic rules without requiring
  //    parserOptions.project, which keeps the lint step fast.
  ...tseslint.configs.recommended,

  // 3) React Hooks rules: `react-hooks/rules-of-hooks` (error) and
  //    `react-hooks/exhaustive-deps` (warn). Appropriate for the React 18 SPA.
  reactHooks.configs['recommended-latest'],

  // 4) React Refresh rule (`react-refresh/only-export-components`) for the Vite
  //    dev server's fast-refresh boundary; a no-op on non-component modules.
  reactRefresh.configs.vite,

  // 5) Application source. Declares the browser/DOM runtime globals the SPA uses
  //    (the `globals` npm package is not a dependency; see the decision log) and
  //    relaxes two rules to warnings so they do not block the lint step.
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        window: 'readonly',
        document: 'readonly',
        navigator: 'readonly',
        console: 'readonly',
        // WebSocket transport (the only channel for game moves) and its events.
        WebSocket: 'readonly',
        MessageEvent: 'readonly',
        CloseEvent: 'readonly',
        Event: 'readonly',
        // Timer and animation-frame APIs used for reconnect backoff, move
        // pacing, and board animations.
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        setInterval: 'readonly',
        clearInterval: 'readonly',
        requestAnimationFrame: 'readonly',
        cancelAnimationFrame: 'readonly',
        // Persistence plus the DOM element types referenced by component refs.
        localStorage: 'readonly',
        HTMLElement: 'readonly',
        HTMLInputElement: 'readonly',
        HTMLDivElement: 'readonly',
      },
    },
    rules: {
      // Unused identifiers are warnings; an underscore prefix opts a binding out.
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // `any` is discouraged but allowed as a warning during active development.
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
);

/**
 * ESLint 9 flat configuration (ES module) for the Blitzy Chess frontend.
 *
 * Entry point for `make lint` (npm run lint -> `eslint .`). It lints the
 * TypeScript / React single-page application source under `src/`.
 *
 * Module system: package.json declares `"type": "module"`, so this file is an
 * ES module. It uses `import` / `export default` and never `require` /
 * `module.exports`.
 *
 * Dependency discipline: the only package imported here is `typescript-eslint`.
 * That single package bundles the TypeScript parser, the `@typescript-eslint`
 * plugin, the typed `config()` flat-config helper, and the `configs.recommended`
 * preset. No other lint packages (`@eslint/js`, `globals`,
 * `eslint-plugin-react-hooks`, `eslint-plugin-react-refresh`) are imported,
 * because they are not declared dependencies in package.json and importing them
 * would break `eslint .` with a module-not-found error.
 *
 * Preset choice: `configs.recommended` is the NON type-checked preset, so it
 * does not require `parserOptions.project`. This keeps the lint step fast and
 * robust regardless of tsconfig path nuances.
 *
 * @see https://typescript-eslint.io/packages/typescript-eslint
 */
import tseslint from 'typescript-eslint';

/**
 * The composed flat-config array, built with the typed `tseslint.config()`
 * helper. Order is significant: global ignores first, then the recommended
 * preset, then the project-specific block that targets the TS/TSX source.
 */
export default tseslint.config(
  // 1) Global ignore patterns. Build output, coverage reports, and installed
  //    dependencies are never linted.
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**'],
  },

  // 2) typescript-eslint's recommended (non type-checked) preset. Supplies the
  //    TypeScript parser plus a pragmatic set of syntactic rules for the TS/TSX
  //    sources without requiring type information.
  ...tseslint.configs.recommended,

  // 3) Project-specific block for the application source. Declares the
  //    browser/DOM runtime environment (the SPA runs in the browser and talks
  //    to the backend over WebSocket) and relaxes two rules to warnings so the
  //    lint step stays usable during active development.
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      // Browser/DOM globals used across the SPA. Declared inline because the
      // `globals` npm package is intentionally not a dependency. Every entry is
      // read-only: application code references but never reassigns these.
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
      // Unused identifiers are warnings rather than errors. An underscore prefix
      // opts a binding out entirely, the common convention for intentionally
      // unused parameters and destructured values.
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // `any` is discouraged but allowed as a warning so it does not block the
      // lint step while the application is under active development.
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
);

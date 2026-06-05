// ESLint flat config. Kept minimal so it depends only on installed packages
// (eslint + typescript-eslint). Source agents may extend with React-specific
// plugins as needed.
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: ['dist', 'node_modules', 'coverage', 'eslint.config.js'],
  },
  ...tseslint.configs.recommended,
);

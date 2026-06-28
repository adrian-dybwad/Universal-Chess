// Security-only ESLint config: the blocking gate for the frontend.
//
// This mirrors the Python toolchain's split (ruff `--select S` is a blocking
// security subset; the full ruff ruleset is report-only). `npm run lint` runs
// the full developer ruleset (style + react-hooks) and currently has a backlog
// of non-security findings, so it is NOT a CI/pre-commit gate. This config
// enables ONLY the XSS/injection rules so the gate fails on a security
// regression - "DOM text reinterpreted as HTML" (CodeQL js/xss-through-dom) and
// dangerouslySetInnerHTML - without being blocked by the style backlog.
//
// Run with:  npm run lint:security
import nounsanitized from 'eslint-plugin-no-unsanitized'
import react from 'eslint-plugin-react'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // innerHTML / insertAdjacentHTML / document.write / createContextualFragment
      nounsanitized.configs.recommended,
    ],
    plugins: {
      react,
    },
    languageOptions: {
      parser: tseslint.parser,
      ecmaVersion: 2020,
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      'react/no-danger': 'error',
    },
  },
])

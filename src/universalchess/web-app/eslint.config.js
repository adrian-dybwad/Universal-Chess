import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import react from 'eslint-plugin-react'
import nounsanitized from 'eslint-plugin-no-unsanitized'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
      // Security: flag XSS sinks so they fail lint before they reach CodeQL.
      // no-unsanitized covers "DOM text reinterpreted as HTML" (innerHTML,
      // insertAdjacentHTML, document.write, Range.createContextualFragment).
      nounsanitized.configs.recommended,
    ],
    plugins: {
      react,
    },
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // React-specific XSS vector (CodeQL js/xss): dangerouslySetInnerHTML.
      'react/no-danger': 'error',
    },
  },
])

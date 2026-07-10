/**
 * Vitest global setup: initialize the app's i18next instance once so any
 * component rendered in a test resolves `t(...)` to real strings (English by
 * default) instead of raw keys. Individual tests can switch the language via
 * `i18n.changeLanguage(...)` or by driving the settings store. Kept as a shared
 * setup file so every test file gets a consistently initialized instance without
 * importing it directly.
 */

import '../i18n';

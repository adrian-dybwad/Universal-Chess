import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

/**
 * Guards against reintroducing a render-blocking external stylesheet import.
 *
 * Regression context: the board is a self-hosted appliance that iPhones reach
 * over the LAN or the board's own Wi-Fi AP, which frequently has no route to
 * the public internet. A CSS `@import url("https://...")` at the top of a
 * render-blocking stylesheet stalls CSSOM construction until that request
 * completes or times out. On such a network the request never completes, so
 * iOS Safari paints nothing (white screen) until a forced repaint. Fonts must
 * be self-hosted; any external `@import` brings the bug back.
 *
 * How the regression manifests here: this test fails the moment a `src/**` CSS
 * file gains an `@import` that points at an http(s) URL, because the assertion
 * below lists every offending file/line.
 */

const thisDir = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(thisDir, '..');

function collectCssFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  const files: string[] = [];
  for (const entry of entries) {
    const fullPath = join(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      files.push(...collectCssFiles(fullPath));
    } else if (entry.endsWith('.css')) {
      files.push(fullPath);
    }
  }
  return files;
}

// Matches a real `@import` statement (anchored to line start, allowing leading
// whitespace) whose target is an absolute http(s) URL. Anchoring avoids false
// positives from the word "@import" appearing inside comment prose. Local
// imports (relative paths, node_modules bare specifiers) are allowed.
const EXTERNAL_IMPORT = /^\s*@import\s+(?:url\(\s*)?['"]?https?:\/\//i;

describe('source CSS', () => {
  it('has no render-blocking external @import (fonts must be self-hosted)', () => {
    const offenders: string[] = [];
    for (const file of collectCssFiles(srcRoot)) {
      const lines = readFileSync(file, 'utf-8').split('\n');
      lines.forEach((line, index) => {
        if (EXTERNAL_IMPORT.test(line)) {
          offenders.push(`${file}:${index + 1}: ${line.trim()}`);
        }
      });
    }
    expect(offenders).toEqual([]);
  });
});

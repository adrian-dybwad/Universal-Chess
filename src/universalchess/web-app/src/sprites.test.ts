import { describe, expect, it } from 'vitest';

import { parseSpriteSheets, spritePreviewPath } from './sprites';

describe('sprite preview urls', () => {
  it('keeps a string catalog usable and unversioned', () => {
    // Why: every settings test, and any response from before the hash, is a
    // list of ids. Dropping those would blank the sprite picker.
    // How a regression manifests: parse returns [] or the preview URL gains
    // an empty ?v=.
    const sheets = parseSpriteSheets(['default', 'onebit']);
    expect(sheets).toEqual([
      { id: 'default', version: '' },
      { id: 'onebit', version: '' },
    ]);
    expect(spritePreviewPath(sheets[0])).toBe('/api/sprites/default/image');
  });

  it('puts the sheet hash on the preview url', () => {
    // Why: the path /api/sprites/default/image does not change when the file
    // does. The hash is what lets the browser keep the picture.
    // How a regression manifests: the URL has no ?v=, so the response stays
    // no-cache and the display page downloads every sheet again.
    const sheets = parseSpriteSheets([{ id: 'default', version: 'abc123' }, { id: '3D', version: 'def456' }]);
    expect(spritePreviewPath(sheets[0])).toBe('/api/sprites/default/image?v=abc123');
    expect(spritePreviewPath(sheets[1])).toBe('/api/sprites/3D/image?v=def456');
  });

  it('falls back to the default sheet when the catalog is empty', () => {
    // Why: a failed or empty list must still offer the built-in sheet.
    // How a regression manifests: the picker has no rows.
    expect(parseSpriteSheets([])).toEqual([{ id: 'default', version: '' }]);
    expect(parseSpriteSheets(null)).toEqual([{ id: 'default', version: '' }]);
  });
});

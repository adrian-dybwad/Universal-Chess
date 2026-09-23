// A sprite sheet the display page can preview. `version` is a hash of the
// sheet file from GET /api/sprites. Empty means the server could not hash it,
// and the preview URL stays unversioned so the browser revalidates.
export interface SpriteSheetRef {
  id: string;
  version: string;
}

/**
 * Accept both the current `{id, version}` catalog and the older string list.
 *
 * Settings tests and any cached client still send `['default']`. Those sheets
 * preview without a hash. A non-array or an empty list falls back to the
 * default sheet so the selector is never blank.
 */
export function parseSpriteSheets(data: unknown): SpriteSheetRef[] {
  if (!Array.isArray(data)) return [{ id: 'default', version: '' }];
  const sheets: SpriteSheetRef[] = [];
  for (const item of data) {
    if (typeof item === 'string' && item) {
      sheets.push({ id: item, version: '' });
    } else if (item && typeof item === 'object' && 'id' in item && typeof item.id === 'string' && item.id) {
      const version = 'version' in item && typeof item.version === 'string' ? item.version : '';
      sheets.push({ id: item.id, version });
    }
  }
  return sheets.length > 0 ? sheets : [{ id: 'default', version: '' }];
}

/** Preview URL. The hash is the cache key; without it the response revalidates. */
export function spritePreviewPath(sheet: SpriteSheetRef): string {
  const path = `/api/sprites/${encodeURIComponent(sheet.id)}/image`;
  return sheet.version ? `${path}?v=${encodeURIComponent(sheet.version)}` : path;
}

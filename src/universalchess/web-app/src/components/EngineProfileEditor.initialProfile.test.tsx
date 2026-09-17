// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { EngineProfileEditor } from './EngineProfileEditor';
import { DEFAULT_PROFILE_ID } from './engineOptions';

/**
 * Guards opening the editor on a named profile rather than the first row.
 *
 * Why this test exists: the Players/Centaur shortcut passes the profile id in
 * use. The editor used to always select the first profile (Default), so that
 * shortcut would show Default's options even when the picker was on a rung.
 * How a regression manifests: the combobox value is Default while
 * initialProfileId was the custom id.
 */

vi.mock('./LoginDialog', () => ({
  LoginDialog: () => null,
}));

const ENGINE = 'berserk';
const PROFILE_ID = 'Profile-a1b2c3';

const SCHEMA_RESPONSE = {
  engine: ENGINE,
  editable: true,
  schema: [],
  profiles: [
    { id: DEFAULT_PROFILE_ID, label: 'Default (Unlimited)', values: {} },
    { id: PROFILE_ID, label: '1400 ELO', values: { UCI_Elo: '1400' } },
  ],
  case_collisions: [],
};

beforeEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => SCHEMA_RESPONSE,
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('EngineProfileEditor initial profile', () => {
  it('selects the given profile id instead of the first in the list', async () => {
    render(
      <EngineProfileEditor
        engineName={ENGINE}
        displayName="Berserk"
        initialProfileId={PROFILE_ID}
        onBack={() => {}}
      />,
    );
    const picker = await screen.findByRole('combobox');
    expect(picker).toHaveValue(PROFILE_ID);
  });

  it('falls back to the first profile when the given id is not in the list', async () => {
    render(
      <EngineProfileEditor
        engineName={ENGINE}
        displayName="Berserk"
        initialProfileId="Profile-missing"
        onBack={() => {}}
      />,
    );
    const picker = await screen.findByRole('combobox');
    expect(picker).toHaveValue(DEFAULT_PROFILE_ID);
  });
});

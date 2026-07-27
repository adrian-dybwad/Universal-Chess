/**
 * Guards Default-profile save-as behavior in the Engines profile editor.
 *
 * Default is seed-owned: editing its fields must not overwrite the section
 * still named Default. Dirty Default -> save requires a new name (create).
 */
import { describe, expect, it } from 'vitest'
import {
  mustSaveDefaultAsNew,
  profileFormIsDirty,
  shouldConfirmProfileReplace,
  findExistingProfileName,
  isReservedProfileName,
  toOverridePayload,
  type Profile,
  type SchemaGroup,
} from './engineOptions'

const SCHEMA: SchemaGroup[] = [
  {
    id: 'strength',
    label: 'Strength',
    fields: [
      { key: 'UCI_LimitStrength', label: 'Limit', type: 'bool', default: false },
      { key: 'UCI_Elo', label: 'Elo', type: 'int', default: 1500, min: 1000, max: 3000 },
    ],
  },
]

const DEFAULT_PROFILE: Profile = {
  name: 'Default',
  values: { UCI_LimitStrength: 'false' },
}

describe('profileFormIsDirty', () => {
  it('is clean when form matches the loaded Default profile (filled defaults)', () => {
    // Why: Save on an untouched Default must stay a no-op, not force save-as.
    // Failure: dirty=true on open would block Save / demand a name incorrectly.
    const form = {
      UCI_LimitStrength: 'false',
      UCI_Elo: '1500', // schema default filled in by valuesForProfile
    }
    expect(profileFormIsDirty(SCHEMA, form, DEFAULT_PROFILE)).toBe(false)
  })

  it('is dirty when Default strength options are changed', () => {
    // Why: changing Maia weights / Stockfish limit on Default means it is no
    // longer the seeded default. Failure: dirty=false would allow overwrite.
    const form = {
      UCI_LimitStrength: 'true',
      UCI_Elo: '1500',
    }
    expect(profileFormIsDirty(SCHEMA, form, DEFAULT_PROFILE)).toBe(true)
  })
})

describe('mustSaveDefaultAsNew', () => {
  it('requires a new name only when Default is selected and the form is dirty', () => {
    // Why: Default stays the seeded anchor; edits save-as under a new name.
    // Failure: false here would POST to /profiles/Default and overwrite it.
    expect(mustSaveDefaultAsNew('Default', false, true)).toBe(true)
    expect(mustSaveDefaultAsNew('Default', false, false)).toBe(false)
    expect(mustSaveDefaultAsNew('1200 ELO', false, true)).toBe(false)
    expect(mustSaveDefaultAsNew('Default', true, true)).toBe(false) // already in new-profile flow
  })
})

describe('isReservedProfileName', () => {
  it('treats Default / DEFAULT / default as reserved (case-insensitive)', () => {
    // Why: ConfigParser keeps case-distinct sections; a twin "default" bypasses
    // seed-owned Default immutability. Failure: false for "default" allows create.
    expect(isReservedProfileName('Default')).toBe(true)
    expect(isReservedProfileName('DEFAULT')).toBe(true)
    expect(isReservedProfileName('default')).toBe(true)
    expect(isReservedProfileName('DeFaUlT')).toBe(true)
    expect(isReservedProfileName('1200 ELO')).toBe(false)
  })
})

describe('findExistingProfileName', () => {
  it('returns the on-disk spelling for a case-insensitive match', () => {
    // Why: save-as "1200 elo" must update "1200 ELO", not add a second section.
    // Failure: undefined here leaves writeName as the typed casing.
    const names = ['Default', '1200 ELO', 'Attacker']
    expect(findExistingProfileName('1200 elo', names)).toBe('1200 ELO')
    expect(findExistingProfileName('attacker', names)).toBe('Attacker')
    expect(findExistingProfileName('Fresh', names)).toBeUndefined()
  })
})

describe('shouldConfirmProfileReplace', () => {
  it('prompts only for create/save-as onto an existing name', () => {
    // Why: "New profile" / Default save-as with name "1200 ELO" must not silently
    // wipe that section. Editing the open "1200 ELO" profile (saveAsNew=false)
    // is an intentional update and must not prompt.
    // Failure: false for save-as onto an existing name = silent overwrite.
    const names = ['Default', '1200 ELO', 'Attacker']
    expect(shouldConfirmProfileReplace(true, '1200 ELO', names)).toBe(true)
    expect(shouldConfirmProfileReplace(true, 'Fresh', names)).toBe(false)
    expect(shouldConfirmProfileReplace(false, '1200 ELO', names)).toBe(false)
    expect(shouldConfirmProfileReplace(true, '', names)).toBe(false)
  })

  it('prompts when the typed name differs only by case from an existing section', () => {
    // Why: case-only variants would otherwise create a near-duplicate section.
    // Failure: false for "1200 elo" = no confirm and a second section on save.
    const names = ['Default', '1200 ELO', 'Attacker']
    expect(shouldConfirmProfileReplace(true, '1200 elo', names)).toBe(true)
    expect(shouldConfirmProfileReplace(true, 'ATTACKER', names)).toBe(true)
  })
})

describe('info fields in form helpers', () => {
  const ABOUT_SCHEMA: SchemaGroup[] = [
    {
      id: 'about',
      label: 'About',
      fields: [
        {
          key: 'UCI_EngineAbout',
          label: 'UCI_EngineAbout',
          type: 'info',
          default: 'see www.example.com',
        },
      ],
    },
    {
      id: 'advanced',
      label: 'Advanced',
      fields: [
        { key: 'Description', label: 'Description', type: 'text', default: '' },
      ],
    },
  ]

  it('never includes info fields in the save payload', () => {
    // Why: about text is display-only; sending it would hit the read-only API
    // reject. Failure: payload contains UCI_EngineAbout.
    const form = {
      UCI_EngineAbout: 'tampered',
      Description: 'Hello',
    }
    expect(toOverridePayload(ABOUT_SCHEMA, form)).toEqual({ Description: 'Hello' })
  })

  it('ignores info fields when deciding dirty', () => {
    // Why: showing the engine default for info must not force save-as on Default.
    // Failure: dirty=true because formValues differ from a stale stored about.
    const form = {
      UCI_EngineAbout: 'anything',
      Description: '',
    }
    expect(profileFormIsDirty(ABOUT_SCHEMA, form, null)).toBe(false)
  })
})

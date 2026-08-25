/**
 * Guards the profile-editor helpers around the reserved Default and the optional
 * profile name.
 *
 * Default is seed-owned: it is re-derived by "Reset profiles" and is the stored
 * strength of every unconfigured player slot, so an edit to it forks a new
 * profile rather than being saved in place. A profile's name is an ordinary
 * edited value with no identity role, which is what these tests pin: the name no
 * longer decides which section is written.
 */
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_PROFILE_ID,
  mustForkDefault,
  nameForPayload,
  orderSchemaGroups,
  profileFormIsDirty,
  profileLabel,
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
  id: DEFAULT_PROFILE_ID,
  label: 'Default (Unlimited)',
  values: { UCI_LimitStrength: 'false' },
}

describe('profileFormIsDirty', () => {
  it('is clean when form matches the loaded Default profile (filled defaults)', () => {
    // Why: Save on an untouched Default must stay a no-op, not fork a profile.
    // Failure: dirty=true on open would create a duplicate of Default on save.
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

describe('mustForkDefault', () => {
  it('forks only when Default is selected and the form is dirty', () => {
    // Why: Default stays the seeded anchor, so an edit to it becomes a new
    // profile under an id the server mints. Failure: false here would POST to
    // /profiles/Default and overwrite the anchor every slot falls back to.
    expect(mustForkDefault(DEFAULT_PROFILE_ID, false, true)).toBe(true)
    expect(mustForkDefault(DEFAULT_PROFILE_ID, false, false)).toBe(false)
    expect(mustForkDefault('Profile-a1b2c3', false, true)).toBe(false)
    // Already creating: the fork decision has been made.
    expect(mustForkDefault(DEFAULT_PROFILE_ID, true, true)).toBe(false)
  })
})

describe('nameForPayload', () => {
  it('sends a name only when it changed, and sends the empty one', () => {
    // Why: an unchanged name must not be rewritten by an ordinary save, while
    // clearing it is a real edit -- it returns the profile to the label projected
    // from its values. Failure: undefined for a cleared name leaves the old name
    // in place, so the profile keeps a name the user deleted.
    expect(nameForPayload('Club Player', '')).toBe('Club Player')
    expect(nameForPayload('  Club Player  ', 'Club Player')).toBeUndefined()
    expect(nameForPayload('', 'Club Player')).toBe('')
    expect(nameForPayload('', undefined)).toBeUndefined()
  })
})

describe('profileLabel', () => {
  it('falls back to the id when the server sent no label', () => {
    // Why: the select must render something for every row; an unlabelled option
    // cannot be picked. Failure: an empty option appears in the profile picker.
    expect(profileLabel(DEFAULT_PROFILE)).toBe('Default (Unlimited)')
    expect(profileLabel({ id: 'Profile-a1b2c3', values: {} })).toBe('Profile-a1b2c3')
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
    // Why: showing the engine default for info must not force a fork of Default.
    // Failure: dirty=true because formValues differ from a stale stored about.
    const form = {
      UCI_EngineAbout: 'anything',
      Description: '',
    }
    expect(profileFormIsDirty(ABOUT_SCHEMA, form, null)).toBe(false)
  })
})

describe('orderSchemaGroups', () => {
  it('moves About ahead of other groups without reordering the rest', () => {
    // Why: About (UCI_EngineAbout) must greet at the top of the editor.
    // Failure: about stays last when API order drifts or is stale.
    const ordered = orderSchemaGroups([
      { id: 'strength', label: 'Strength', fields: [] },
      { id: 'advanced', label: 'Advanced', fields: [] },
      { id: 'about', label: 'About', fields: [] },
    ])
    expect(ordered.map((g) => g.id)).toEqual(['about', 'strength', 'advanced'])
  })

  it('is a no-op when there is no About group', () => {
    const ordered = orderSchemaGroups([
      { id: 'strength', label: 'Strength', fields: [] },
      { id: 'engine', label: 'Engine', fields: [] },
    ])
    expect(ordered.map((g) => g.id)).toEqual(['strength', 'engine'])
  })
})

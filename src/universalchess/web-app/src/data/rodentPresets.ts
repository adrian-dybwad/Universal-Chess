/**
 * Character presets and playing-style modifiers for Rodent IV personalities.
 *
 * These mirror the personalities shipped with Rodent IV's own tuner (Pawel
 * Koziol's distribution) and the playing-style tweaks from the legacy Flask
 * tuner. They are keyed by engine name so the otherwise schema-driven profile
 * editor stays generic: an engine without an entry simply shows no preset bar.
 *
 * Provenance / encoding notes:
 * - Every key here must exist in the backend profile schema
 *   (services/engine_profiles.py); values must fall inside that schema's
 *   ranges, since the backend rejects out-of-range writes.
 * - PrimaryPstStyle / SecondaryPstStyle are the engine's integer PST codes
 *   (0=Quirky, 1=Classic, 2=Normal, 3=Blunt, 4=Forward), not the style names
 *   used by the old tuner UI.
 * - GuideBookFile values are paths relative to the engine's books/ folder
 *   (the engine resolves books under books/ itself). Only books that differ
 *   from the engine default (guide.bin) are listed; the referenced files ship
 *   in the install's books/guide/ directory. Note the Strangler book file is
 *   capitalised (guide/Strangler.bin) on a case-sensitive filesystem.
 */

export interface ProfilePreset {
  /** Stable id for React keys and button identity. */
  id: string;
  /** Suggested profile name, prefilled when composing a new profile. */
  name: string;
  /** Human description; also written to the profile's Description field. */
  description: string;
  /** Sparse overrides keyed by schema key (everything else stays at default). */
  values: Record<string, number | string>;
}

export interface PlayingStyle {
  id: string;
  name: string;
  description: string;
  /** Partial overrides layered on top of the current form values. */
  values: Record<string, number>;
}

const RODENT_PRESETS: ProfilePreset[] = [
  {
    id: 'ampere',
    name: 'Ampere',
    description: 'Attacker that still cares for pawn structure.',
    values: {
      OwnAttack: 160, OwnMobility: 100, OppMobility: 70, FlatMobility: 25,
      Space: 100, PassedPawns: 125, PawnStructure: 150,
      DoubledPawnMg: -10, BackwardPawnMg: -4, BackwardPawnEg: -3, BackwardOnOpenMg: -9,
      PrimaryPstStyle: 2, SecondaryPstStyle: 2,
      GuideBookFile: 'guide/active.bin',
    },
  },
  {
    id: 'cloe',
    name: 'Cloe',
    description: 'Likes closed positions and a solid pawn wall.',
    values: {
      PawnShield: 150, PawnStorm: 120, OppMobility: 60,
      Lines: 120, Outposts: 90, Space: 100,
      PawnMass: 150, PawnChains: 150, KnightLikesClosed: 7, KeepPawn: 1,
      PrimaryPstStyle: 2,
      GuideBookFile: 'guide/closed.bin',
    },
  },
  {
    id: 'deborah',
    name: 'Deborah',
    description: 'Defensive player who likes bishops.',
    values: {
      BishopValueMg: 400, BishopValueEg: 380,
      OwnAttack: 85, OppAttack: 115, PiecePressure: 125,
      KnightLikesClosed: 5, RookLikesOpen: 3,
      PrimaryPstStyle: 1, SecondaryPstStyle: 0,
    },
  },
  {
    id: 'grumpy',
    name: 'Grumpy',
    description: 'Attack and restraint with contempt; likes blocked positions.',
    values: {
      KnightValueMg: 385, KnightValueEg: 365, BishopPairMg: 41,
      OwnAttack: 120, OppMobility: 70, Space: 100,
      KnightLikesClosed: 8, KeepPawn: 1,
      GuideBookFile: 'guide/grandpa.bin',
    },
  },
  {
    id: 'strangler',
    name: 'Strangler',
    description: 'Positional squeeze that restricts the opponent.',
    values: {
      RookValueMg: 500, RookValueEg: 620,
      OwnAttack: 300, KingTropism: 35, PawnShield: 120,
      Material: 90, OppMobility: 150, PiecePressure: 125,
      Outposts: 125, Space: 100, KnightLikesClosed: 8,
      KeepKnight: 4, KeepQueen: 5,
      DoubledPawnMg: -12, DoubledPawnEg: -24,
      IsolatedPawnMg: -10, IsolatedPawnEg: -10,
      BackwardPawnMg: -8, BackwardPawnEg: -10, BackwardOnOpenMg: -8,
      Contempt: 5,
      GuideBookFile: 'guide/Strangler.bin',
    },
  },
  {
    id: 'swapper',
    name: 'Swapper',
    description: 'Likes exchanging pieces and steering toward a draw.',
    values: {
      PawnStructure: 120, Contempt: -20,
    },
  },
];

const RODENT_STYLES: PlayingStyle[] = [
  {
    id: 'attacker',
    name: 'Attacker',
    description: 'Press the attack and stay active.',
    values: { OwnAttack: 125, OppAttack: 100, OwnMobility: 75, OppMobility: 50 },
  },
  {
    id: 'defender',
    name: 'Defender',
    description: 'Weight the opponent\u2019s threats and your own safety.',
    values: { OwnAttack: 100, OppAttack: 125, OwnMobility: 50, OppMobility: 75 },
  },
  {
    id: 'constrictor',
    name: 'Constrictor',
    description: 'Attack while limiting your own piece activity.',
    values: { OwnAttack: 125, OppAttack: 100, OwnMobility: 50, OppMobility: 75 },
  },
  {
    id: 'escapist',
    name: 'Escapist',
    description: 'Stay mobile while respecting opponent threats.',
    values: { OwnAttack: 100, OppAttack: 125, OwnMobility: 75, OppMobility: 50 },
  },
];

export const PRESETS_BY_ENGINE: Record<string, ProfilePreset[]> = {
  rodentIV: RODENT_PRESETS,
};

export const STYLES_BY_ENGINE: Record<string, PlayingStyle[]> = {
  rodentIV: RODENT_STYLES,
};

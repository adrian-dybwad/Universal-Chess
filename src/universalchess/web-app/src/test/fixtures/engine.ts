import type { EngineDefinition } from '../../types/game';

/**
 * Builds an `EngineDefinition` as `GET /api/engines/all` returns one.
 *
 * Shared because the shape is the API's, not any one test's. Seven test files
 * each carried their own copy of this literal, so adding a field to the payload
 * broke all seven at once and each had to be brought back in line by hand --
 * which is also how a copy drifts from the real response without anything
 * noticing. Defining it here means a new field is added once and every test
 * keeps describing a response the server could actually send.
 *
 * The defaults describe an engine that is not installed. Tests that need an
 * installed one pass `installedEngine` overrides or set the fields themselves;
 * either way what a test cares about is visible at its own call site rather than
 * buried in a base object.
 */
export function makeEngine(overrides: Partial<EngineDefinition> = {}): EngineDefinition {
  return {
    name: 'placeholder',
    display_name: 'Placeholder',
    description: 'desc',
    summary: 'summary',
    info_url: '',
    installed: false,
    has_prebuilt: false,
    estimated_install_minutes: 0,
    has_profiles: false,
    profiles_ready: false,
    last_failure: null,
    needs_repair: false,
    can_repair: false,
    missing_net_count: 0,
    supported: true,
    unsupported_reason: null,
    source_installable: true,
    recommended_ref: null,
    installed_ref: null,
    resume_point: null,
    tier: 'specialty',
    is_system_package: false,
    ...overrides,
  };
}

/**
 * An installed, healthy engine: its binary is present and its strength ladder
 * was seeded, so the profile editor is offered.
 *
 * Kept alongside `makeEngine` because "installed" is three fields that must
 * agree, and a test that sets only `installed` describes an engine the server
 * never returns -- one whose card claims to be ready while nothing behind it
 * works.
 */
export function installedEngine(overrides: Partial<EngineDefinition> = {}): EngineDefinition {
  return makeEngine({
    installed: true,
    has_profiles: true,
    profiles_ready: true,
    ...overrides,
  });
}

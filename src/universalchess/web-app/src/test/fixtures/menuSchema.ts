import catalog from '../../../../menus/catalog/menu.json';
import runtimeOptionSets from './runtimeOptionSets.json';

/**
 * The menu schema as `GET /api/menu-schema` serves it, derived from the real
 * catalog rather than copied from it.
 *
 * This used to be a checked-in copy of `menu.json`. Nothing compared the two,
 * so every catalog edit had to be made twice by hand and the suite silently
 * kept asserting against a catalog the product no longer had. Importing the
 * real file means a catalog change is exercised immediately, and there is no
 * second copy to forget.
 *
 * `time_control_presets` is the exception the server still injects: it builds
 * that list per request from the time-control catalog, so it is absent from
 * `menu.json` and is supplied here to match what the UI receives.
 * `timezones` used to be injected the same way. The product now loads that
 * list from GET /api/system/timezones when the System tab is open. The fixture
 * still carries a short list so a test that renders the selector without
 * mocking that endpoint has options; a test of the endpoint itself strips it.
 */
export const menuSchemaFixture = {
  ...catalog,
  optionSets: {
    ...catalog.optionSets,
    ...runtimeOptionSets,
  },
};

export default menuSchemaFixture;

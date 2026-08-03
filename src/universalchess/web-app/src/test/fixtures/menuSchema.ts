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
 * `timezones` and `time_control_presets` are the exception: the server builds
 * them per request from the device (the IANA database and the time-control
 * catalog), so they are absent from `menu.json` and are supplied here to match
 * what the UI actually receives.
 */
export const menuSchemaFixture = {
  ...catalog,
  optionSets: {
    ...catalog.optionSets,
    ...runtimeOptionSets,
  },
};

export default menuSchemaFixture;

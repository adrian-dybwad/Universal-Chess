// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards how the opt-in deep-analysis engine layers over the board's numbers.
 *
 * The board's evaluation is what every install gets; the CDN engine is an
 * override for the one position the user is looking at. Two properties matter:
 * it must not run at all unless the user opted in (it costs a 39 MB download
 * from a CDN), and when it does run, a failure must leave the board's
 * evaluation on screen rather than blanking it.
 *
 * How a regression manifests
 * --------------------------
 * Loading the engine regardless of the setting spends 39 MB of a metered or
 * absent connection that the user never agreed to. Replacing the board's value with
 * null on a failed load blanks the eval bar and the best-move arrow for users
 * on a LAN with no internet -- the exact configuration this product targets.
 */

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1';

const analyzeMock = vi.fn();
const destroyMock = vi.fn();
vi.mock('../services/stockfish', () => ({
  StockfishService: class {
    analyze = (...args: unknown[]) => analyzeMock(...args);
    destroy = () => destroyMock();
  },
  getStockfishService: () => ({ analyze: analyzeMock, destroy: destroyMock }),
  destroyStockfishService: () => destroyMock(),
}));

vi.mock('react-chartjs-2', () => ({ Line: () => <div data-testid="chart" /> }));

let rawSettings: Record<string, Record<string, string>> | null = null;
vi.mock('../stores/settingsStore', () => ({
  useSettingsStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      raw: rawSettings,
      loaded: true,
      revision: 1,
      load: async () => {},
    }),
  settingKey: (section: string, key: string) => `${section}.${key}`,
}));

import { Analysis } from './Analysis';

const POSITIONS = [
  { fen: START_FEN, san: null, uci: null, eval: null, best_move: null },
  { fen: AFTER_E4, san: 'e4', uci: 'e2e4', eval: 120, best_move: 'e7e5' },
];

beforeEach(() => {
  analyzeMock.mockReset();
  destroyMock.mockReset();
  rawSettings = { game: { deep_analysis: 'False' } };
});

afterEach(() => cleanup());

describe('Analysis deep analysis override', () => {
  it('runs no engine when the setting is off', async () => {
    // The default install must contact nobody. Regression: loading the engine
    // regardless of the setting starts a 39 MB CDN download on an appliance the
    // user expects to be offline -- and the CSP would block it, so the only
    // visible symptom is a console error.
    render(<Analysis positions={POSITIONS} mode="static" />);

    await screen.findByText('+1.2');
    expect(analyzeMock).not.toHaveBeenCalled();
    // Releasing on the off path is what keeps a user who turns the setting back
    // off from leaving the ~39 MB of engine blobs resident for the session.
    expect(destroyMock).toHaveBeenCalled();
  });

  it('shows the deep engine result for the viewed position when opted in', async () => {
    // The whole point of opting in: a stronger evaluation than a 0.3s Pi search.
    // Regression: ignoring the returned score leaves the board's +1.2 on screen
    // and the download bought nothing.
    //
    // The score also exercises the perspective flip. AFTER_E4 is Black to move,
    // so UCI's -250 (bad for Black) is +2.5 for White. Omitting the flip shows
    // -2.5 here -- a sign error that silently inverts every other ply of the
    // chart, which is why the fixture is deliberately not a Black-to-move-neutral
    // value.
    rawSettings = { game: { deep_analysis: 'True' } };
    analyzeMock.mockResolvedValue({
      fen: AFTER_E4, score: -250, mate: null, bestMove: 'g8f6', depth: 20,
    });

    render(<Analysis positions={POSITIONS} mode="static" />);

    expect(await screen.findByText('+2.5')).toBeInTheDocument();
    expect(analyzeMock.mock.calls[0][0]).toBe(AFTER_E4);
  });

  it.each(['True', 'true', 'yes', 'on', '1'])(
    'treats the persisted value %s as opted in',
    async (persisted) => {
      // configparser.getboolean accepts all of these, so the board and the
      // Settings page both read them as on. A reader matching only "true"
      // diverges: the server widens the CSP and the toggle shows enabled while
      // the review page silently never runs the engine, which looks like the
      // download failing rather than a parse disagreement.
      rawSettings = { game: { deep_analysis: persisted } };
      analyzeMock.mockResolvedValue({
        fen: AFTER_E4, score: -250, mate: null, bestMove: 'g8f6', depth: 20,
      });

      render(<Analysis positions={POSITIONS} mode="static" />);

      expect(await screen.findByText('+2.5')).toBeInTheDocument();
    },
  );

  it.each(['False', 'false', 'no', 'off', '0', ''])(
    'treats the persisted value "%s" as opted out',
    async (persisted) => {
      // The mirror of the above: an unrecognised-as-false value must not start
      // a 39 MB download. Regression shows up as an engine load on an install
      // whose Settings page reads "off".
      rawSettings = { game: { deep_analysis: persisted } };

      render(<Analysis positions={POSITIONS} mode="static" />);

      await screen.findByText('+1.2');
      expect(analyzeMock).not.toHaveBeenCalled();
    },
  );

  it('keeps the board evaluation when the engine fails to load', async () => {
    // LAN-only installs, an offline board, or a CDN outage all land here. The
    // board's own number is still correct and must stay visible; regression
    // manifests as a blank eval and no best-move arrow for exactly the users
    // who never had internet in the first place.
    rawSettings = { game: { deep_analysis: 'True' } };
    analyzeMock.mockRejectedValue(new Error('checksum mismatch'));

    render(<Analysis positions={POSITIONS} mode="static" />);

    await waitFor(() => expect(analyzeMock).toHaveBeenCalled());
    // Let the rejection and any state update it triggers flush before asserting,
    // so this cannot pass merely by reading the screen before the failure lands.
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('+1.2')).toBeInTheDocument();
  });
});

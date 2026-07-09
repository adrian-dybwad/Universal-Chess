// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards the non-modal Board Control panel: it renders only when open, shows the
 * live e-paper screen and the six physical buttons, posts key presses to
 * /api/board/key, and closes via its close button. Being non-modal, it must NOT
 * render a full-screen backdrop that would block the page behind it.
 */

const apiFetchMock = vi.fn();
vi.mock('../utils/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));

vi.mock('./LoginDialog', () => ({
  LoginDialog: () => null,
}));

import { BoardControlPanel } from './BoardControlPanel';
import { publishSseEvent, __resetSseBus } from '../utils/sseBus';

beforeEach(() => {
  __resetSseBus();
  apiFetchMock.mockReset();
  apiFetchMock.mockResolvedValue({ status: 200, ok: true, json: async () => ({ success: true }) });
  // jsdom does not implement pointer capture; the buttons call it on press.
  // Stub it so the pointer handlers run without throwing (environment boundary).
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
});

afterEach(() => {
  cleanup();
});

describe('BoardControlPanel', () => {
  it('renders nothing when closed', () => {
    // Closed means the panel body (and its /screen.jpg <img>) is not rendered,
    // so the snapshot is not fetched in the background; the "Board display"
    // image must be absent.
    render(<BoardControlPanel isOpen={false} onClose={() => {}} />);
    expect(screen.queryByAltText('Board display')).not.toBeInTheDocument();
  });

  it('shows the live e-paper screen from a cache-busted /screen.jpg when open', () => {
    // Open must show the e-paper screen sourced from the static /screen.jpg
    // snapshot (not the old MJPEG /screen stream, which iPad Safari will not
    // render inside an <img>). The src carries a ?t= cache-buster.
    render(<BoardControlPanel isOpen onClose={() => {}} />);
    const screenImg = screen.getByAltText('Board display');
    expect(screenImg).toBeInTheDocument();
    expect(screenImg.getAttribute('src')).toMatch(/^\/screen\.jpg\?t=/);
  });

  it('reloads the screen with the new mtime when an epaper_changed event arrives', () => {
    // An epaper_changed SSE event must bump the <img> src's ?t= token to the
    // event's mtime so the browser fetches the fresh snapshot exactly once.
    // Regression: if the handler ignored the event, the src would not change and
    // the mirror would freeze on the first frame despite the board refreshing.
    render(<BoardControlPanel isOpen onClose={() => {}} />);
    const before = screen.getByAltText('Board display').getAttribute('src');

    act(() => {
      publishSseEvent('epaper_changed', { mtime: 1712345678.5 });
    });

    const after = screen.getByAltText('Board display').getAttribute('src');
    expect(after).not.toBe(before);
    expect(after).toBe('/screen.jpg?t=1712345678.5');
  });

  it('renders the six physical control buttons', () => {
    // All six device buttons must be present; a broken layout could drop the group.
    render(<BoardControlPanel isOpen onClose={() => {}} />);
    for (const name of ['Up', 'Back', 'Ok / Menu', 'Down', 'Hint']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: /Play \/ Pause/ })).toBeInTheDocument();
  });

  it('posts a short key press to /api/board/key', async () => {
    // Tapping a button (pointer down then up quickly) must post that key with
    // long_press false. A regression in press classification or the endpoint
    // would send the wrong body or nothing.
    render(<BoardControlPanel isOpen onClose={() => {}} />);
    const back = screen.getByRole('button', { name: 'Back' });
    fireEvent.pointerDown(back, { pointerId: 1 });
    fireEvent.pointerUp(back, { pointerId: 1 });

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));
    const [path, init] = apiFetchMock.mock.calls[0];
    expect(path).toBe('/api/board/key');
    expect(init.requiresAuth).toBe(true);
    expect(JSON.parse(init.body)).toEqual({ key: 'BACK', long_press: false });
  });

  it('closes via the close button', () => {
    // The close control must invoke onClose so the navbar can hide the panel.
    const onClose = vi.fn();
    render(<BoardControlPanel isOpen onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: /close board control/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

"""Tests for the derived-engine Stockfish loader.

Background / why these tests exist
----------------------------------
The derived engines (Worstfish, Drawfish) have no evaluation of their own; they
drive the installed Stockfish, opened by ``open_stockfish``. Two behaviours here
are load-bearing and were the subject of a field bug on the dgt-64 board:

* Stockfish must be opened with ``timeout=None``. python-chess's default
  ``popen_uci`` handshake timeout is 10s, and Stockfish's ``uci`` initialization
  can exceed that on a constrained board, raising ``TimeoutError`` so the engine
  never opened and the derived engine produced no move. The rest of the app
  (``EngineRegistry``) already opens engines with ``timeout=None``; this loader
  must match.
* A missing Stockfish must fail loudly with ``RuntimeError`` rather than a later,
  more confusing UCI error.

``chess.engine.SimpleEngine.popen_uci`` is the external boundary (it spawns a
real subprocess), so it is patched here; everything else is the real code.
"""

import chess.engine
import pytest

from universalchess.services.derived_engines import stockfish


def test_open_stockfish_disables_handshake_timeout(monkeypatch):
    """`open_stockfish` must call popen_uci with `timeout=None`.

    Why: python-chess defaults to a 10s handshake timeout, which Stockfish's
    `uci` init exceeded on dgt-64, so `popen_uci` raised `TimeoutError` and no
    backing engine opened (the derived engine then produced no `bestmove`). How
    the regression manifests: if the explicit `timeout=None` were dropped, the
    recorded kwargs would carry no `timeout` (or a numeric one) and this asserts
    exactly the value that disables the cap. The path resolver is stubbed so the
    test does not depend on a real Stockfish install.
    """
    captured = {}

    def fake_popen_uci(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return object()  # stand-in engine; open_stockfish only returns it

    monkeypatch.setattr(stockfish, "resolve_stockfish_path", lambda: "/usr/games/stockfish")
    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(fake_popen_uci))

    engine = stockfish.open_stockfish()

    assert engine is not None
    assert captured["command"] == "/usr/games/stockfish"
    assert "timeout" in captured["kwargs"]
    assert captured["kwargs"]["timeout"] is None  # disables the 10s default cap


def test_open_stockfish_raises_when_not_found(monkeypatch):
    """A missing Stockfish must raise RuntimeError, not open anything.

    Why: the derived engines cannot function without Stockfish; failing loudly
    at open time is clearer than a later UCI error. How the regression
    manifests: if the empty-path guard were removed, `popen_uci` would be called
    with "" and raise a confusing lower-level error instead of this explicit one.
    """
    monkeypatch.setattr(stockfish, "resolve_stockfish_path", lambda: "")

    def fail_popen_uci(command, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("popen_uci must not be called when Stockfish is absent")

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(fail_popen_uci))

    with pytest.raises(RuntimeError):
        stockfish.open_stockfish()

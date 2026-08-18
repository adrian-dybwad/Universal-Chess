"""Tests for the handles that live only as long as one game.

These exist because the application used to hold the running game as seven
module-level names and clear them one at a time. Nothing checked that the
teardown reached all of them, or that it reached the later ones when an earlier
component's cleanup raised -- a handle left alive there draws on a display the
next game does not own.
"""

import dataclasses

import pytest

from universalchess.app.game_runtime import GameRuntime


class _Component:
    """A game component that records its teardown against a shared log."""

    def __init__(self, name: str, calls: list, *, raises: bool = False):
        self._name = name
        self._calls = calls
        self._raises = raises

    def _record(self):
        self._calls.append(self._name)
        if self._raises:
            raise RuntimeError(f"{self._name} failed to close")

    def cleanup(self):
        self._record()

    def close(self):
        self._record()


@pytest.fixture
def calls():
    return []


@pytest.fixture
def runtime(calls):
    """A runtime holding every component, each recording its own teardown."""
    return GameRuntime(
        protocol=_Component("protocol", calls),
        display=_Component("display", calls),
        controller=_Component("controller", calls),
        coach=object(),
        lichess_session=_Component("lichess", calls),
        is_position_game=True,
        player_signature=("stockfish", "human"),
    )


class TestClose:
    def test_the_components_are_torn_down_in_the_order_they_require(
        self, runtime, calls
    ):
        # The order is a real constraint, not an accident of statement order: the
        # Lichess session holds the started-splash timer and must release it
        # before the display it would draw on goes away, the controller must stop
        # routing board events before the game handling them is dismantled, and
        # the display is last because the other two draw on it. A regression that
        # reorders them shows up here as a different sequence, where before this
        # test it showed up on the board as game widgets painted over the menu.
        runtime.close()

        assert calls == ["lichess", "controller", "protocol", "display"]

    def test_one_component_failing_does_not_strand_the_others(self, calls):
        # A cleanup that raises used to be caught per component; if that
        # isolation is lost, the components after the failing one are never told
        # to close and stay alive with no code path that will ever close them.
        # The failure manifests as a shorter call list: teardown stopped early.
        runtime = GameRuntime(
            protocol=_Component("protocol", calls),
            display=_Component("display", calls),
            controller=_Component("controller", calls, raises=True),
            lichess_session=_Component("lichess", calls),
        )

        runtime.close()

        assert calls == ["lichess", "controller", "protocol", "display"]
        assert runtime.protocol is None
        assert runtime.display is None
        assert runtime.controller is None
        assert runtime.lichess_session is None

    def test_every_handle_returns_to_its_default(self, runtime):
        # The bug being guarded is a field added to the runtime and forgotten by
        # the teardown, which is how a stale handle survived into the next game.
        # Asserting against the declared defaults rather than a hand-written list
        # means a new field is covered the moment it is declared; a teardown that
        # skips one fails here with that field still holding the old game's value.
        runtime.close()

        for field in dataclasses.fields(GameRuntime):
            assert getattr(runtime, field.name) == field.default, (
                f"{field.name} survived teardown"
            )

    def test_closing_an_empty_runtime_does_nothing(self, calls):
        # The no-game case: teardown runs after a start that failed before
        # building anything, and on the second of two closes. Calling cleanup on
        # None would raise AttributeError and abort the caller's return to the
        # menu, leaving the board on a game screen with no game.
        runtime = GameRuntime()

        runtime.close()
        runtime.close()

        assert calls == []
        assert not runtime.is_running


class TestIsRunning:
    def test_a_live_protocol_means_a_game_is_running(self, calls):
        # The application treats a live protocol behind the menu as "a resumable
        # game exists", which is what PLAY and the RESUME relabel consult. If this
        # stops tracking the protocol handle, PLAY either discards a game in
        # progress or resumes one that has been torn down.
        runtime = GameRuntime()
        assert not runtime.is_running

        runtime.protocol = _Component("protocol", calls)
        assert runtime.is_running

        runtime.close()
        assert not runtime.is_running

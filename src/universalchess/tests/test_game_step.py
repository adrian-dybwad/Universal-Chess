"""Tests for which deferred game work the main loop performs on a pass.

The choice was a bare elif chain inside the loop, so the priority among five
kinds of work existed only as the order someone happened to write them in, and
nothing checked that losing an election left the other work still pending. Both
halves matter: rebuilding widgets for players that are about to be torn down
wastes a full panel refresh, and a claim that discards the work it did not do
loses a web settings change with no trace.
"""

import itertools

import pytest

from universalchess.app.game_step import GameAction, claim_game_step
from universalchess.app.pending_work import PendingWork

# Slot name -> the action requesting it should produce, in the priority order the
# loop applies. Ordered highest-priority first, which the tests below rely on.
ACTION_FOR_SLOT = (
    ("switch_to_normal_game", GameAction.SWITCH_TO_NORMAL_GAME),
    ("player_rebuild", GameAction.REBUILD_PLAYERS),
    ("layout_rebuild", GameAction.REBUILD_LAYOUT),
    ("settings_reload", GameAction.RELOAD_SETTINGS),
    ("board_command", GameAction.APPLY_BOARD_COMMAND),
)

SLOT_NAMES = [name for name, _ in ACTION_FOR_SLOT]

# The board command is cleared by the code that applies it, not by the claim, so
# it is the one slot still pending after being chosen. See claim_game_step.
PEEKED_SLOTS = {"board_command"}


@pytest.fixture
def pending():
    return PendingWork()


class TestAnIdleGame:
    def test_nothing_pending_asks_for_nothing(self, pending):
        # The overwhelmingly common pass: the loop must fall through to its sleep
        # rather than acting. An action here would rebuild the display every pass.
        assert claim_game_step(pending).action is GameAction.IDLE

    def test_a_lichess_reason_alone_is_not_work(self, pending):
        # lichess_next only colours a rebuild's prompt; it is not work on its own.
        # Treating it as work would open the next-game menu unprompted, and
        # consuming it here would rob the rebuild that arrives moments later of
        # the reason the remote game ended.
        pending.lichess_next.request("aborted")

        assert claim_game_step(pending).action is GameAction.IDLE
        assert pending.lichess_next.requested()


class TestASingleRequest:
    @pytest.mark.parametrize(("slot_name", "action"), ACTION_FOR_SLOT)
    def test_each_kind_of_work_maps_to_its_action(self, pending, slot_name, action):
        # Each slot on its own reaches the loop as the action that services it. A
        # mapping that crossed two of these would perform the wrong repair and
        # leave the real request pending forever.
        getattr(pending, slot_name).request()

        assert claim_game_step(pending).action is action

    @pytest.mark.parametrize("slot_name", SLOT_NAMES)
    def test_the_chosen_work_is_claimed_so_it_runs_once(self, pending, slot_name):
        # A claim that does not consume repeats the work on every pass -- for a
        # player rebuild that is an endless game-restart loop the user cannot
        # escape. The board command is the documented exception.
        getattr(pending, slot_name).request()
        claim_game_step(pending)

        still_pending = getattr(pending, slot_name).requested()
        assert still_pending is (slot_name in PEEKED_SLOTS)


class TestPriority:
    def test_a_new_game_outranks_every_repair(self, pending):
        # With everything set at once, the switch to a normal game wins: the
        # others repair a game that is about to be replaced, so doing one first
        # spends a panel refresh on state that is discarded.
        for slot_name in SLOT_NAMES:
            getattr(pending, slot_name).request()

        assert claim_game_step(pending).action is GameAction.SWITCH_TO_NORMAL_GAME

    @pytest.mark.parametrize(
        ("higher", "lower"), list(itertools.combinations(SLOT_NAMES, 2))
    )
    def test_the_higher_ranked_of_any_pair_wins(self, pending, higher, lower):
        # Every pairing, requested in the losing order so a dispatcher that simply
        # honours arrival order fails. ACTION_FOR_SLOT is the intended ranking, so
        # this pins the whole ordering rather than the two extremes.
        getattr(pending, lower).request()
        getattr(pending, higher).request()

        expected = dict(ACTION_FOR_SLOT)[higher]
        assert claim_game_step(pending).action is expected

    @pytest.mark.parametrize(
        ("higher", "lower"), list(itertools.combinations(SLOT_NAMES, 2))
    )
    def test_losing_work_survives_for_the_next_pass(self, pending, higher, lower):
        # The half that is easy to get wrong. Claiming every slot while acting on
        # one silently drops the rest: a web settings change made in the same
        # moment as a rebuild would never be applied and would look like the web
        # UI failing to save.
        getattr(pending, lower).request()
        getattr(pending, higher).request()
        claim_game_step(pending)

        assert getattr(pending, lower).requested()


class TestTheLichessReason:
    def test_a_rebuild_carries_why_the_remote_game_ended(self, pending):
        # The reason decides whether the next-game prompt asks to seek again or
        # reports an abort, so it must travel with the rebuild. Losing it makes an
        # aborted Lichess game look like an ordinary finished one.
        pending.player_rebuild.request()
        pending.lichess_next.request("noStart")

        step = claim_game_step(pending)

        assert step.action is GameAction.REBUILD_PLAYERS
        assert step.lichess_reason == "noStart"
        # Claimed with the rebuild: left pending, it would label the game after next.
        assert not pending.lichess_next.requested()

    def test_a_rebuild_without_a_reason_is_an_ordinary_one(self, pending):
        # The common local case -- an engine changed from the web. None is what
        # tells the prompt to offer a new seek rather than explain a termination.
        pending.player_rebuild.request()

        step = claim_game_step(pending)

        assert step.action is GameAction.REBUILD_PLAYERS
        assert step.lichess_reason is None

    @pytest.mark.parametrize("slot_name", [n for n in SLOT_NAMES if n != "player_rebuild"])
    def test_other_work_leaves_the_reason_alone(self, pending, slot_name):
        # Only a rebuild consults it. Any other action consuming it would strip
        # the reason from the rebuild that follows on a later pass.
        getattr(pending, slot_name).request()
        pending.lichess_next.request("aborted")

        step = claim_game_step(pending)

        assert step.lichess_reason is None
        assert pending.lichess_next.requested()


class TestTheContract:
    def test_every_action_except_idle_is_produced_by_some_slot(self):
        # Guards the enum against an action added with no way to reach it, which
        # would be dead code in the loop's dispatch table.
        produced = {action for _, action in ACTION_FOR_SLOT}
        assert produced == set(GameAction) - {GameAction.IDLE}

"""Tests for the controllers module.

Tests cover:
- GameController abstract base class
- LocalController initialization and event routing
- RemoteController initialization and protocol detection
- ControllerManager switching and event routing

Test Approach:
- Mock GameManager to avoid actual game logic
- Test event routing and state transitions
- Verify controller switching behavior
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, PropertyMock


class TestGameControllerBase(unittest.TestCase):
    """Test cases for GameController abstract base class."""
    
    def test_cannot_instantiate_abstract_class(self):
        """Test that GameController cannot be instantiated directly.
        
        Expected: Attempting to instantiate raises TypeError.
        Failure: Abstract base class is incorrectly concrete.
        """
        from universalchess.controllers.base import GameController
        
        mock_game_manager = MagicMock()
        
        with self.assertRaises(TypeError):
            GameController(mock_game_manager)
    
    def test_has_required_abstract_methods(self):
        """Test that GameController defines required abstract methods.
        
        Expected: Abstract methods start, stop, on_field_event, on_key_event exist.
        Failure: Interface contract is incomplete.
        """
        from universalchess.controllers.base import GameController
        import abc
        
        # Check that abstract methods are defined
        abstract_methods = getattr(GameController, '__abstractmethods__', set())
        
        assert 'start' in abstract_methods
        assert 'stop' in abstract_methods
        assert 'on_field_event' in abstract_methods
        assert 'on_key_event' in abstract_methods


class TestLocalController(unittest.TestCase):
    """Test cases for LocalController."""
    
    def test_init_creates_inactive_controller(self):
        """Test that LocalController starts inactive.
        
        Expected: is_active should be False after initialization.
        Failure: Controller starts in wrong state.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        assert controller.is_active is False
    
    def test_start_activates_controller(self):
        """Test that start() activates the controller.
        
        Expected: is_active should be True after start().
        Failure: Controller doesn't activate properly.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        controller.start()
        
        assert controller.is_active is True
    
    def test_stop_deactivates_controller(self):
        """Test that stop() deactivates the controller.
        
        Expected: is_active should be False after stop().
        Failure: Controller doesn't deactivate properly.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        controller.start()
        controller.stop()
        
        assert controller.is_active is False
    
    def test_field_event_routes_to_game_manager_when_active(self):
        """Test that field events route to GameManager when active.
        
        Expected: receive_field should be called on GameManager.
        Failure: Field events not routed correctly.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        controller.start()
        controller.on_field_event(0, 12, 1.5)  # piece_event=0 (lift), field=12, time=1.5
        
        mock_game_manager.receive_field.assert_called_once_with(0, 12, 1.5)
    
    def test_field_event_ignored_when_inactive(self):
        """Test that field events are ignored when inactive.
        
        Expected: receive_field should NOT be called when controller is inactive.
        Failure: Events processed when controller should be paused.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        # Don't call start()
        controller.on_field_event(0, 12, 1.5)
        
        mock_game_manager.receive_field.assert_not_called()
    
    def test_key_event_routes_to_game_manager_when_active(self):
        """Test that key events route to GameManager when active.
        
        Expected: receive_key should be called on GameManager.
        Failure: Key events not routed correctly.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        mock_key = MagicMock()
        controller.start()
        controller.on_key_event(mock_key)
        
        mock_game_manager.receive_key.assert_called_once_with(mock_key)
    
    def test_key_event_ignored_when_inactive(self):
        """Test that key events are ignored when inactive.
        
        Expected: receive_key should NOT be called when controller is inactive.
        Failure: Events processed when controller should be paused.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        mock_key = MagicMock()
        # Don't call start()
        controller.on_key_event(mock_key)
        
        mock_game_manager.receive_key.assert_not_called()
    
    def test_set_player_manager(self):
        """Test setting the player manager.
        
        Expected: player_manager property should return the set manager.
        Failure: Player manager not stored correctly.
        
        Note: This test requires Raspberry Pi hardware modules (spidev).
        The actual functionality works correctly on the target platform.
        """
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        controller = LocalController(mock_game_manager)
        
        mock_player_manager = MagicMock()
        mock_player_manager.white_player = MagicMock()
        mock_player_manager.white_player.name = "Human"
        mock_player_manager.black_player = MagicMock()
        mock_player_manager.black_player.name = "Engine"
        
        # Mock players to not be LichessPlayer instances
        mock_player_manager.white_player.__class__ = type('HumanPlayer', (), {})
        mock_player_manager.black_player.__class__ = type('EnginePlayer', (), {})
        
        try:
            controller.set_player_manager(mock_player_manager)
            assert controller.player_manager is mock_player_manager
        except ModuleNotFoundError as e:
            if 'spidev' in str(e) or 'RPi' in str(e):
                self.skipTest("Requires Raspberry Pi hardware modules")
    
    def test_local_controller_has_no_lichess_property(self):
        """LocalController must not name a player plugin.

        Why: is_lichess was isinstance LichessPlayer with no production
        callers. Rebuild vs in-place is requires_rebuild_on_new_game.

        Failure: is_lichess is back, so the controller still special-cases
        Lichess instead of the remote-session capability.
        """
        from universalchess.controllers.local import LocalController

        assert not hasattr(LocalController, "is_lichess")
        controller = LocalController(MagicMock())
        assert not hasattr(controller, "is_lichess")

    def test_new_game_requests_move_when_rebuild_not_required(self):
        """An engine/human board-reset still starts the next game in place.

        Why: EVENT_NEW_GAME must keep requesting a move after on_new_game for
        local opponents; a player still attached to an external game skips that.

        How the regression manifests: _request_current_player_move is not called,
        so an engine game after a board-reset never asks White to move.
        """
        from universalchess.controllers.local import LocalController
        from universalchess.managers.events import EVENT_NEW_GAME

        controller = LocalController(MagicMock())
        controller._player_manager = MagicMock()
        controller._player_manager.requires_rebuild_on_new_game = False
        controller._request_current_player_move = MagicMock()
        controller._check_assistant_suggestion = MagicMock()

        controller._on_game_event(EVENT_NEW_GAME)

        controller._player_manager.on_new_game.assert_called_once()
        controller._request_current_player_move.assert_called_once()

    def test_new_game_with_remote_does_not_request_in_place_move(self):
        """Board-reset during remote play must not request a move from the old stream.

        Why: EVENT_NEW_GAME always called _request_current_player_move after
        on_new_game. For engines that starts the next game in place; for a
        player still on a remote game, a move request would be against that
        abandoned game while the local board is at start.

        How the regression manifests: _request_current_player_move is called.
        """
        from universalchess.controllers.local import LocalController
        from universalchess.managers.events import EVENT_NEW_GAME

        controller = LocalController(MagicMock())
        controller._player_manager = MagicMock()
        controller._player_manager.requires_rebuild_on_new_game = True
        controller._request_current_player_move = MagicMock()
        controller._check_assistant_suggestion = MagicMock()

        controller._on_game_event(EVENT_NEW_GAME)

        controller._player_manager.on_new_game.assert_called_once()
        controller._request_current_player_move.assert_not_called()


class TestRemoteController(unittest.TestCase):
    """Test cases for RemoteController."""
    
    def test_init_creates_inactive_controller(self):
        """Test that RemoteController starts inactive.
        
        Expected: is_active should be False after initialization.
        Failure: Controller starts in wrong state.
        """
        from universalchess.controllers.remote import RemoteController
        
        mock_game_manager = MagicMock()
        controller = RemoteController(mock_game_manager)
        
        assert controller.is_active is False
    
    def test_start_activates_controller_and_creates_emulators(self):
        """Test that start() activates and creates emulators.
        
        Expected: is_active should be True after start().
        Failure: Controller doesn't initialize properly.
        """
        from universalchess.controllers.remote import RemoteController
        
        mock_game_manager = MagicMock()
        controller = RemoteController(mock_game_manager)
        
        # Mock _create_emulators to avoid actual emulator imports
        with patch.object(controller, '_create_emulators'):
            controller.start()
            
            assert controller.is_active is True
    
    def test_stop_deactivates_controller(self):
        """Test that stop() deactivates the controller.
        
        Expected: is_active should be False after stop().
        Failure: Controller doesn't deactivate properly.
        """
        from universalchess.controllers.remote import RemoteController
        
        mock_game_manager = MagicMock()
        controller = RemoteController(mock_game_manager)
        
        with patch.object(controller, '_create_emulators'):
            controller.start()
        
        controller.stop()
        
        assert controller.is_active is False
    
    def test_client_type_unknown_initially(self):
        """Test that client_type is unknown before protocol detection.
        
        Expected: client_type should be 'unknown' initially.
        Failure: Wrong initial client type.
        """
        from universalchess.controllers.remote import RemoteController, CLIENT_UNKNOWN
        
        mock_game_manager = MagicMock()
        controller = RemoteController(mock_game_manager)
        
        assert controller.client_type == CLIENT_UNKNOWN
    
    def test_is_protocol_detected_false_initially(self):
        """Test that is_protocol_detected is False initially.
        
        Expected: No protocol should be detected before data arrives.
        Failure: Protocol detection state wrong.
        """
        from universalchess.controllers.remote import RemoteController
        
        mock_game_manager = MagicMock()
        controller = RemoteController(mock_game_manager)
        
        assert controller.is_protocol_detected is False
    
    def test_set_client_type_hint(self):
        """Test setting client type hint.
        
        Expected: Hint should be stored for protocol detection priority.
        Failure: Hint not stored correctly.
        """
        from universalchess.controllers.remote import RemoteController, CLIENT_MILLENNIUM
        
        mock_game_manager = MagicMock()
        controller = RemoteController(mock_game_manager)
        
        controller.set_client_type_hint(CLIENT_MILLENNIUM)
        
        assert controller._client_type_hint == CLIENT_MILLENNIUM


class TestControllerManager(unittest.TestCase):
    """Test cases for ControllerManager."""
    
    def test_init_no_controllers(self):
        """Test that ControllerManager starts with no controllers.
        
        Expected: Both local and remote controllers should be None initially.
        Failure: Controllers created prematurely.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        assert manager.local_controller is None
        assert manager.remote_controller is None
        assert manager.active_controller is None
    
    def test_create_local_controller(self):
        """Test creating a local controller.
        
        Expected: local_controller should be set after creation.
        Failure: Controller not created correctly.
        """
        from universalchess.controllers.manager import ControllerManager
        from universalchess.controllers.local import LocalController
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        controller = manager.create_local_controller()
        
        assert controller is not None
        assert isinstance(controller, LocalController)
        assert manager.local_controller is controller
    
    def test_create_remote_controller(self):
        """Test creating a remote controller.
        
        Expected: remote_controller should be set after creation.
        Failure: Controller not created correctly.
        """
        from universalchess.controllers.manager import ControllerManager
        from universalchess.controllers.remote import RemoteController
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        mock_send_callback = MagicMock()
        controller = manager.create_remote_controller(mock_send_callback)
        
        assert controller is not None
        assert isinstance(controller, RemoteController)
        assert manager.remote_controller is controller
    
    def test_activate_local(self):
        """Test activating the local controller.
        
        Expected: local controller should become active.
        Failure: Controller activation failed.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        manager.create_local_controller()
        manager.activate_local()
        
        assert manager.is_local_active is True
        assert manager.is_remote_active is False
    
    def test_activate_remote(self):
        """Test activating the remote controller.
        
        Expected: remote controller should become active.
        Failure: Controller activation failed.
        """
        from universalchess.controllers.manager import ControllerManager
        from universalchess.controllers.remote import RemoteController
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        remote = manager.create_remote_controller()
        with patch.object(remote, '_create_emulators'):
            manager.activate_remote()
        
        assert manager.is_remote_active is True
        assert manager.is_local_active is False
    
    def test_switching_stops_previous_controller(self):
        """Test that switching controllers stops the previous one.
        
        Expected: Previous controller should be stopped when new one activates.
        Failure: Multiple controllers active simultaneously.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        local = manager.create_local_controller()
        remote = manager.create_remote_controller()
        
        manager.activate_local()
        assert local.is_active is True
        
        with patch.object(remote, '_create_emulators'):
            manager.activate_remote()
        
        # Local should now be inactive
        assert local.is_active is False
    
    def test_on_field_event_routes_to_active(self):
        """Test that field events route to active controller.
        
        Expected: Event should be sent to active controller's on_field_event.
        Failure: Events not routed correctly.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        local = manager.create_local_controller()
        manager.activate_local()
        
        manager.on_field_event(0, 12, 1.5)
        
        # Field event should have been routed to game manager via local controller
        mock_game_manager.receive_field.assert_called_with(0, 12, 1.5)
    
    def test_on_key_event_routes_to_active(self):
        """Test that key events route to active controller.
        
        Expected: Event should be sent to active controller's on_key_event.
        Failure: Events not routed correctly.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        local = manager.create_local_controller()
        manager.activate_local()
        
        mock_key = MagicMock()
        manager.on_key_event(mock_key)
        
        # Key event should have been routed to game manager via local controller
        mock_game_manager.receive_key.assert_called_with(mock_key)
    
    def test_deactivate_all(self):
        """Test deactivating all controllers.
        
        Expected: No controller should be active after deactivate_all.
        Failure: Controllers still active after deactivation.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        local = manager.create_local_controller()
        manager.activate_local()
        assert manager.is_local_active is True
        
        manager.deactivate_all()
        
        assert manager.active_controller is None
        assert local.is_active is False
    
    def test_on_controller_change_callback(self):
        """Test that controller change callback is called.
        
        Expected: Callback should be called with is_remote flag.
        Failure: Callback not invoked on controller change.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        callback = MagicMock()
        manager.set_on_controller_change(callback)
        
        manager.create_local_controller()
        manager.activate_local()
        
        callback.assert_called_once_with(False)  # False = not remote
        
        callback.reset_mock()
        
        remote = manager.create_remote_controller()
        with patch.object(remote, '_create_emulators'):
            manager.activate_remote()
        
        callback.assert_called_once_with(True)  # True = remote


class TestControllerManagerBluetoothHandling(unittest.TestCase):
    """Test cases for ControllerManager Bluetooth handling."""
    
    def test_on_bluetooth_disconnected_activates_local(self):
        """Test that Bluetooth disconnect reactivates local controller.
        
        Expected: Local controller should be activated on BT disconnect.
        Failure: Local controller not reactivated after BT disconnect.
        """
        from universalchess.controllers.manager import ControllerManager
        
        mock_game_manager = MagicMock()
        manager = ControllerManager(mock_game_manager)
        
        manager.create_local_controller()
        remote = manager.create_remote_controller()
        
        with patch.object(remote, '_create_emulators'):
            manager.activate_remote()
        
        assert manager.is_remote_active is True
        
        with patch.object(remote, '_create_emulators'):
            manager.on_bluetooth_disconnected()
        
        assert manager.is_local_active is True
        assert manager.is_remote_active is False


if __name__ == '__main__':
    unittest.main()

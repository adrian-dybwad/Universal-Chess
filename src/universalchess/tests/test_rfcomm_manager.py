"""Tests for rfcomm_manager.py"""
from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import threading
import time


class TestRfcommManager(unittest.TestCase):
    """Test cases for RfcommManager"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Speed up tests by disabling real sleeps inside rfcomm manager methods.
        self._sleep_patcher = patch("universalchess.managers.rfcomm.time.sleep", return_value=None)
        self._sleep_patcher.start()

    def tearDown(self):
        if hasattr(self, "_sleep_patcher"):
            self._sleep_patcher.stop()
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    def test_enable_bluetooth(self, mock_process_iter, mock_popen):
        """Test enabling Bluetooth and making device discoverable"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_popen.return_value = mock_proc
        
        # Mock process iteration for bt-agent check
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        controller.enable_bluetooth()
        
        # Verify bluetoothctl commands were sent
        assert mock_proc.stdin.write.called
        write_calls = [str(call_args[0][0]) for call_args in mock_proc.stdin.write.call_args_list]
        assert any("power on" in str(call) for call in write_calls)
        assert any("discoverable on" in str(call) for call in write_calls)
        assert any("pairable on" in str(call) for call in write_calls)
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    @patch('universalchess.managers.rfcomm.RfcommManager._read_controller_inquiry_classes', return_value={})
    @patch('universalchess.managers.rfcomm.RfcommManager._discover_bredr_with_btmgmt', return_value=[])
    @patch('universalchess.managers.rfcomm.RfcommManager._discover_keyboards_from_inquiry_classes', return_value=[])
    def test_start_discovery(self, _inquiry, _btmgmt, _classes, mock_process_iter, mock_popen):
        """Test starting device discovery"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess with a realistic, terminating scan stream.
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.side_effect = [
            "Discovery started\n",
            "[\x1b[0;92mNEW\x1b[0m] Device 49:71:2D:41:07:E3 Logi K380\n",
            "",  # terminates the read loop
        ]
        mock_popen.return_value = mock_proc
        
        # Mock process iteration
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        # Force the readline path (MagicMock stdout is not a pollable fd).
        with patch("universalchess.managers.rfcomm.select.poll", side_effect=Exception):
            devices = controller.start_discovery(timeout=5)
        
        # Verify scan on was called
        assert mock_proc.stdin.write.called
        write_calls = [str(call_args[0][0]) for call_args in mock_proc.stdin.write.call_args_list]
        assert any("scan on" in str(call) for call in write_calls)
        # And the ANSI-tagged [NEW] line is actually parsed into a device.
        assert devices == [{"address": "49:71:2D:41:07:E3", "name": "Logi K380"}]
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    @patch('select.poll')
    def test_start_pairing_classic_bluetooth(self, mock_poll, mock_process_iter, mock_popen):
        """Test starting pairing for Classic Bluetooth"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess for bluetoothctl
        mock_btctl_proc = MagicMock()
        mock_btctl_proc.stdin = MagicMock()
        mock_btctl_proc.stdout = MagicMock()
        
        # Mock subprocess for bt-agent
        mock_agent_proc = MagicMock()
        mock_agent_proc.stdin = MagicMock()
        mock_agent_proc.stdout = MagicMock()
        mock_agent_proc.poll.return_value = None
        
        def popen_side_effect(*args, **kwargs):
            if 'bluetoothctl' in str(args[0]):
                return mock_btctl_proc
            elif 'bt-agent' in str(args[0]):
                return mock_agent_proc
            return MagicMock()
        
        mock_popen.side_effect = popen_side_effect
        
        # Mock poll
        mock_poll_obj = MagicMock()
        mock_poll_obj.poll.return_value = []
        mock_poll.return_value = mock_poll_obj
        
        # Mock process iteration
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        result = controller.start_pairing(timeout=1)
        
        # Should return False due to timeout (no device detected)
        assert result is False
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    def test_set_device_name(self, mock_process_iter, mock_popen):
        """Test setting Bluetooth device name"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_popen.return_value = mock_proc
        
        # Mock process iteration
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        controller.set_device_name("TEST DEVICE")
        
        # Verify system-alias command was sent
        assert mock_proc.stdin.write.called
        write_calls = [str(call_args[0][0]) for call_args in mock_proc.stdin.write.call_args_list]
        assert any("system-alias" in str(call) for call in write_calls)
        assert any("TEST DEVICE" in str(call) for call in write_calls)
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    def test_get_paired_devices(self, mock_process_iter, mock_popen):
        """Test getting list of paired devices"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess with output
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.side_effect = [
            b'Device AA:BB:CC:DD:EE:FF Test Device\n',
            b'Device 11:22:33:44:55:66 Another Device\n',
            b'[bluetooth]#\n',
            b''
        ]
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        # Mock process iteration
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        devices = controller.get_paired_devices()
        
        # Should return list of devices
        assert isinstance(devices, list)
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    def test_remove_device(self, mock_process_iter, mock_popen):
        """Test removing a paired device"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_popen.return_value = mock_proc
        
        # Mock process iteration
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        controller.remove_device("AA:BB:CC:DD:EE:FF")
        
        # Verify remove command was sent
        assert mock_proc.stdin.write.called
        write_calls = [str(call_args[0][0]) for call_args in mock_proc.stdin.write.call_args_list]
        assert any("remove" in str(call).lower() for call in write_calls)
        assert any("AA:BB:CC:DD:EE:FF" in str(call) for call in write_calls)
    
    @patch('subprocess.Popen')
    @patch('universalchess.managers.rfcomm._process_iter')
    def test_keep_discoverable(self, mock_process_iter, mock_popen):
        """Test keeping device discoverable"""
        from universalchess.managers import RfcommManager
        
        # Mock subprocess
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_popen.return_value = mock_proc
        
        # Mock process iteration
        mock_process_iter.return_value = []
        
        controller = RfcommManager()
        controller.keep_discoverable("TEST DEVICE")
        
        # Verify discoverable commands were sent
        assert mock_proc.stdin.write.called
        write_calls = [str(call_args[0][0]) for call_args in mock_proc.stdin.write.call_args_list]
        assert any("discoverable on" in str(call) for call in write_calls)
        assert any("pairable on" in str(call) for call in write_calls)


if __name__ == '__main__':
    unittest.main()


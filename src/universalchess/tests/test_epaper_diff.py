#!/usr/bin/env python3
"""
Unit tests for compute_changed_region logic without hardware dependencies.

Tests verify that the compute_changed_region function correctly identifies
the row range that changed between two display buffers.

USAGE:
  cd /opt/universalchess
  python3 -m pytest universalchess/tests/test_epaper_diff.py -v
"""

import unittest


def compute_changed_region(prev_bytes: bytes, curr_bytes: bytes) -> tuple[int, int]:
    """
    Compute the row range that changed between two display buffers.
    
    This is a copy of compute_changed_region to avoid importing epaper.py
    (which initializes hardware).
    
    Args:
        prev_bytes: Previous display buffer contents.
        curr_bytes: Current display buffer contents.
        
    Returns:
        Tuple of (start_row, end_row) indicating the changed region.
        Returns (0, 295) for full refresh when buffers are invalid or mismatched.
    """
    if not prev_bytes or not curr_bytes or len(prev_bytes) != len(curr_bytes):
        return 0, 295
    total = len(curr_bytes)
    rs, re = 0, 295
    for i in range(total):
        if prev_bytes[i] != curr_bytes[i]:
            rs = (i // 16) - 1
            break
    for i in range(total - 1, -1, -1):
        if prev_bytes[i] != curr_bytes[i]:
            re = (i // 16) + 1
            break
    if rs < 0:
        rs = 0
    if re > 295:
        re = 295
    if rs >= re:
        return 0, 295
    return rs, re


class TestComputeChangedRegion(unittest.TestCase):
    """
    Tests for compute_changed_region function.
    
    Expected behavior:
    - Empty or mismatched buffers return full range (0, 295)
    - Single-byte changes at start should return range starting at 0
    - Single-byte changes at end should return range ending at 295
    - Middle changes should return appropriate bounded range
    - No changes result in full range fallback
    """
    
    # Display constants
    ROWS = 296
    BYTES_PER_ROW = 16
    TOTAL = ROWS * BYTES_PER_ROW
    
    def setUp(self):
        """Create base buffers for testing."""
        self.empty = b''
        self.buffer_a = bytearray([0xFF] * self.TOTAL)
    
    def test_empty_previous_buffer_returns_full_range(self):
        """
        Test that empty previous buffer returns full range.
        
        Expected: (0, 295) full range for empty previous buffer.
        Failure indicates: Edge case for empty buffer not handled correctly.
        """
        rs, re = compute_changed_region(self.empty, bytes(self.buffer_a))
        self.assertEqual((rs, re), (0, 295))
    
    def test_different_buffer_lengths_returns_full_range(self):
        """
        Test that buffers of different lengths return full range.
        
        Expected: (0, 295) full range when buffers have different lengths.
        Failure indicates: Length mismatch check is broken.
        """
        rs, re = compute_changed_region(bytes(self.buffer_a), bytes(self.buffer_a[:-1]))
        self.assertEqual((rs, re), (0, 295))
    
    def test_single_byte_change_at_start_returns_row_zero(self):
        """
        Test single-byte change at buffer start.
        
        Expected: rs == 0 (first row), re in [0, 2] (small range).
        Failure indicates: Start detection algorithm is broken.
        """
        buffer_b = bytearray(self.buffer_a)
        buffer_b[0] = 0x00
        rs, re = compute_changed_region(bytes(self.buffer_a), bytes(buffer_b))
        self.assertEqual(rs, 0, "Start row should be 0 for change at first byte")
        self.assertGreaterEqual(re, 0, "End row should be >= 0")
        self.assertLessEqual(re, 2, "End row should be <= 2 for change at first byte")
    
    def test_single_byte_change_at_end_returns_row_295(self):
        """
        Test single-byte change at buffer end.
        
        Expected: rs >= 293 (near end), re == 295 (clamped to max).
        Failure indicates: End detection or clamping is broken.
        """
        buffer_b = bytearray(self.buffer_a)
        buffer_b[-1] = 0x00
        rs, re = compute_changed_region(bytes(self.buffer_a), bytes(buffer_b))
        self.assertGreaterEqual(rs, 293, "Start row should be near end for change at last byte")
        self.assertEqual(re, 295, "End row should be clamped to 295")
    
    def test_single_byte_change_in_middle_returns_bounded_range(self):
        """
        Test single-byte change in middle of buffer.
        
        Expected: Range should contain the changed row (123).
        Failure indicates: Middle detection algorithm is broken.
        """
        mid_row = 123
        idx = mid_row * self.BYTES_PER_ROW + 7
        buffer_b = bytearray(self.buffer_a)
        buffer_b[idx] = 0x00
        rs, re = compute_changed_region(bytes(self.buffer_a), bytes(buffer_b))
        self.assertLessEqual(rs, mid_row, "Start row should be <= changed row")
        self.assertGreaterEqual(re, mid_row, "End row should be >= changed row")
    
    def test_no_change_returns_full_range_fallback(self):
        """
        Test identical buffers return full range.
        
        Expected: (0, 295) full range when no changes detected.
        Failure indicates: Equality path fallback is not implemented correctly.
        Note: This is correct behavior - when rs >= re (no change found),
        the function returns full range as a safe fallback.
        """
        rs, re = compute_changed_region(bytes(self.buffer_a), bytes(self.buffer_a))
        self.assertEqual((rs, re), (0, 295))


if __name__ == "__main__":
    unittest.main(verbosity=2)
